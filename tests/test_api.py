"""端到端行为测试，使用 TestClient + 临时 SQLite 文件库。

覆盖：
  * 创建、工作稿各段版本 / 状态流转
  * 依据过期版本的提交与确认被拒绝
  * 原文改版仅令该段已确认译文失效（stale），他段不受影响
  * 发布的修订冲突、阻塞段返回、失败不增加版号
  * 定版不可变：后续改写 / 复核后旧版号读取内容不变
  * 发布与原文改写并发：每次成功发布都与某个完整工作稿修订严格一致
"""

from __future__ import annotations

import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    # 确保 app 模块在设置环境变量之后读取 DB_PATH
    import importlib

    import app.db as db_mod
    import app.main as main_mod

    importlib.reload(db_mod)
    importlib.reload(main_mod)
    db_mod.init_db()
    with TestClient(main_mod.app) as c:
        yield c


def _create(client, n=2, title="示例"):
    r = client.post(
        "/manuscripts",
        json={"title": title, "segments": [f"原文{i}" for i in range(n)]},
    )
    assert r.status_code == 201, r.text
    return r.json()["manuscript_id"]


def _submit(client, mid, pos, text, ver):
    return client.put(
        f"/manuscripts/{mid}/segments/{pos}/translation",
        json={"text": text, "source_version": ver},
    )


def _confirm(client, mid, pos, ver):
    return client.post(
        f"/manuscripts/{mid}/segments/{pos}/confirmation",
        json={"source_version": ver},
    )


def _publish(client, mid, rev):
    return client.post(
        f"/manuscripts/{mid}/publications", json={"expected_revision": rev}
    )


def _reorder(client, mid, segments, expected_revision):
    return client.put(
        f"/manuscripts/{mid}/segments/reorder",
        json={"expected_revision": expected_revision, "segments": segments},
    )


# ---- 基本流程 -------------------------------------------------------------


def test_create_and_draft_states(client):
    mid = _create(client, 2)
    r = client.get(f"/manuscripts/{mid}/draft")
    assert r.status_code == 200
    draft = r.json()
    assert draft["revision"] == 1
    assert [s["position"] for s in draft["segments"]] == [0, 1]
    assert all(s["source_version"] == 1 for s in draft["segments"])
    assert all(s["status"] == "missing" for s in draft["segments"])


def test_submit_confirm_flow(client):
    mid = _create(client)
    assert _submit(client, mid, 0, "trans-0", 1).status_code == 200
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["segments"][0]["status"] == "submitted"
    assert draft["segments"][1]["status"] == "missing"

    assert _confirm(client, mid, 0, 1).status_code == 200
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["segments"][0]["status"] == "confirmed"
    assert draft["segments"][0]["translation"]["target_text"] == "trans-0"

    # 再次提交同一版本：译文被改写，确认状态回到 submitted
    assert _submit(client, mid, 0, "trans-0-new", 1).status_code == 200
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["segments"][0]["status"] == "submitted"
    assert draft["segments"][0]["translation"]["target_text"] == "trans-0-new"


# ---- 过期版本 -------------------------------------------------------------


def test_submit_and_confirm_against_stale_version_rejected(client):
    mid = _create(client)
    _submit(client, mid, 0, "old", 1)
    _confirm(client, mid, 0, 1)

    # 原文改版 -> 修订号与段版本各 +1
    r = client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-新版", "expected_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["source_version"] == 2
    assert r.json()["revision"] == 2

    # 依据旧版本提交 / 确认都被拒绝
    assert _submit(client, mid, 0, "x", 1).status_code == 409
    assert _confirm(client, mid, 0, 1).status_code == 409
    # 依据不存在的未来版本同样拒绝
    assert _submit(client, mid, 0, "x", 3).status_code == 409


def test_rewrite_only_invalidates_that_segment(client):
    mid = _create(client, 3)
    for i in range(3):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)

    r = client.put(
        f"/manuscripts/{mid}/segments/1/source",
        json={"text": "原文1-改", "expected_revision": 1},
    )
    assert r.status_code == 200

    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == 2
    statuses = {s["position"]: s for s in draft["segments"]}
    assert statuses[0]["status"] == "confirmed"          # 他段不受影响
    assert statuses[2]["status"] == "confirmed"
    assert statuses[1]["status"] == "stale"              # 该段待复核
    assert statuses[1]["source_version"] == 2
    assert statuses[1]["stale_translation"]["target_text"] == "t1"
    assert statuses[1]["stale_translation"]["status"] == "stale"


def test_rewrite_revision_conflict_and_idempotence(client):
    mid = _create(client)
    r = client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "A", "expected_revision": 99},
    )
    assert r.status_code == 409

    # 内容相同：不产生新版本、不加修订号
    r = client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0", "expected_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert client.get(f"/manuscripts/{mid}").json()["revision"] == 1


# ---- 发布 ----------------------------------------------------------------


def test_publish_blocks_and_does_not_bump_edition(client):
    mid = _create(client, 2)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)
    # 第 1 段只有 submitted，没有 confirmed

    # 修订号不匹配
    assert _publish(client, mid, 5).status_code == 409

    # 匹配但存在阻塞段
    r = _publish(client, mid, 1)
    assert r.status_code == 422
    assert r.json()["detail"]["blocking_segments"] == [{"position": 1, "source_version": 1}]

    # 没有产生任何定版
    assert client.get(f"/manuscripts/{mid}/publications").json() == []


def test_publish_success_and_immutable_snapshot(client):
    mid = _create(client, 2)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)
    _submit(client, mid, 1, "t1", 1)
    _confirm(client, mid, 1, 1)

    r = _publish(client, mid, 1)
    assert r.status_code == 201, r.text
    pub1 = r.json()
    assert pub1["edition"] == 1
    assert pub1["revision"] == 1
    assert [s["target_text"] for s in pub1["segments"]] == ["t0", "t1"]

    # 发布后改写原文、重新翻译确认、再发布第二版
    client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-v2", "expected_revision": 1},
    )
    _submit(client, mid, 0, "t0-v2", 2)
    _confirm(client, mid, 0, 2)
    r = _publish(client, mid, 2)
    assert r.status_code == 201
    assert r.json()["edition"] == 2

    # 旧版号读取结果不受后续修改 / 复核影响
    old = client.get(f"/manuscripts/{mid}/publications/1").json()
    assert old["revision"] == 1
    assert old["segments"][0]["source_text"] == "原文0"
    assert old["segments"][0]["target_text"] == "t0"

    assert client.get(f"/manuscripts/{mid}/publications/9").status_code == 404


def test_revision_moves_under_unconfirmed_publish(client):
    # 发布与改写交错时只有两种合法结局，都不允许混入不同修订的数据：
    #   1) 改写先提交 -> 发布 409；
    #   2) 发布先提交 -> 定版 201（定格 rev1），改写随后仍可基于 rev1 成功
    #      （发布不推进工作稿修订号）。
    mid = _create(client, 1)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)

    barrier = threading.Barrier(2)

    def rewrite():
        barrier.wait()
        r = client.put(
            f"/manuscripts/{mid}/segments/0/source",
            json={"text": "原文0-new", "expected_revision": 1},
        )
        return r.status_code, r.json() if r.status_code == 200 else None

    def publish():
        barrier.wait()
        r = _publish(client, mid, 1)
        return r.status_code, r.json() if r.status_code == 201 else None

    with ThreadPoolExecutor(max_workers=2) as ex:
        rw, pb = list(ex.map(lambda f: f(), [rewrite, publish]))

    rw_code, _ = rw
    pb_code, pb_body = pb
    assert rw_code in (200, 409)
    assert pb_code in (201, 409)
    assert (rw_code, pb_code) in {(200, 409), (200, 201), (409, 201)}

    pubs = client.get(f"/manuscripts/{mid}/publications").json()
    if pb_code == 201:
        # 定版只能定格 rev1 的原文，绝不能出现改写后的文本
        assert pb_body["revision"] == 1
        assert pb_body["segments"][0]["source_text"] == "原文0"
        assert len(pubs) == 1
        assert pubs[0]["segments"][0]["source_text"] == "原文0"
    else:
        assert pubs == []


def test_concurrent_rewrite_and_publish_never_mix_revisions(client):
    """反复并发：成功的定版快照必须与某个真实工作稿修订整体一致。"""
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}-v1", 1)
        _confirm(client, mid, i, 1)

    outcomes = []

    def publish_once(expected_rev):
        r = _publish(client, mid, expected_rev)
        return ("pub", r.status_code, r.json() if r.status_code == 201 else None)

    def rewrite_once(expected_rev, pos, text):
        r = client.put(
            f"/manuscripts/{mid}/segments/{pos}/source",
            json={"text": text, "expected_revision": expected_rev},
        )
        return ("rewrite", r.status_code, r.json() if r.status_code == 200 else None)

    # 场景：rev1 两段齐备；rev2 改第 0 段（第 1 段未重新确认）。
    # rev2 下发布必须 422；成功发布只能定格 rev1 的全文。
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [
            ex.submit(publish_once, 1),
            ex.submit(rewrite_once, 1, 0, "src0-v2"),
        ]
        for f in futs:
            outcomes.append(f.result())

    for kind, code, body in outcomes:
        if kind == "pub" and code == 201:
            assert body["revision"] == 1
            assert body["segments"][0]["source_text"] == "原文0"
            assert body["segments"][1]["source_text"] == "原文1"

    # 若发布抢在改写前成功，改写后仍可基于 rev2 看到阻塞段
    if any(k == "rewrite" and c == 200 for k, c, _ in outcomes):
        r = _publish(client, mid, 2)
        assert r.status_code == 422
        assert r.json()["detail"]["blocking_segments"] == [{"position": 0, "source_version": 2}]


def test_stress_concurrent_rewrite_publish_snapshots_consistent(client):
    """多轮压测：工作稿 rev N 下只确认第 0 段的新译文（第 1 段保持旧确认），
    则任何成功定版都必须完整对应某一已知修订，绝不允许跨修订拼装。"""
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}-v1", 1)
        _confirm(client, mid, i, 1)

    revision = 1
    version0 = 1
    lock = threading.Lock()

    def do_round(_):
        nonlocal revision, version0
        with lock:
            expected = revision
            version0 += 1
            new_text = f"src0-v{version0}"
            new_target = f"t0-v{version0}"

        rewrite_code = client.put(
            f"/manuscripts/{mid}/segments/0/source",
            json={"text": new_text, "expected_revision": expected},
        ).status_code
        with lock:
            if rewrite_code == 200:
                revision = expected + 1
                current_version0 = version0
            else:
                current_version0 = None

        pub_code = client.post(
            f"/manuscripts/{mid}/publications", json={"expected_revision": expected}
        ).status_code

        # 无论先后，422（段不齐）与 409（修订漂移）都可能出现；
        # 201 仅在 rev 仍为 expected 且两段都匹配当前版本时合法。
        assert pub_code in (201, 409, 422)
        assert rewrite_code in (200, 409)
        if pub_code == 201:
            body = client.get(
                f"/manuscripts/{mid}/publications",
            ).json()
            latest = body[-1]
            assert latest["revision"] == expected
            seg0, seg1 = latest["segments"]
            # 定版瞬间：要么两段都停在 v1，要么两段一致地处于更新后的状态
            assert seg1["source_version"] == 1 and seg1["target_text"] == "t1-v1"
            if rewrite_code == 409:
                assert seg0["source_version"] == 1
            else:
                # 改写已提交，201 只可能发生在"改写之前"，故定格仍是 v1
                assert seg0["source_version"] == 1
        # 改写成功后补确认，让下一轮有更多可发布的交错点
        if current_version0 is not None:
            _submit(client, mid, 0, new_target, current_version0)
            _confirm(client, mid, 0, current_version0)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(do_round, range(15)))

    # 最终每一版定版快照都是自洽的：第 1 段永远停在它被确认的样子，
    # 第 0 段与快照 revision 对应；逐版读取不可变。
    pubs = client.get(f"/manuscripts/{mid}/publications").json()
    for pub in pubs:
        again = client.get(
            f"/manuscripts/{mid}/publications/{pub['edition']}"
        ).json()
        assert again == pub



def test_validation_errors(client):
    r = client.post("/manuscripts", json={"title": "", "segments": ["a"]})
    assert r.status_code == 422
    r = client.post("/manuscripts", json={"title": "x", "segments": []})
    assert r.status_code == 422
    r = client.post("/manuscripts", json={"title": "x", "segments": ["   "]})
    assert r.status_code == 422
    assert client.get("/manuscripts/999").status_code == 404


# ---- 段落编排 -------------------------------------------------------------


def test_reorder_keeps_carries_version_translation_and_status(client):
    # 三段：第 0 段已确认（版本 2），第 1 段仅提交，第 2 段缺失
    mid = _create(client, 3)
    _submit(client, mid, 0, "t0-v1", 1)
    _confirm(client, mid, 0, 1)
    client.put(
        f"/manuscripts/{mid}/segments/0/source",
        json={"text": "原文0-v2", "expected_revision": 1},
    )
    _submit(client, mid, 0, "t0-v2", 2)
    _confirm(client, mid, 0, 2)
    _submit(client, mid, 1, "t1", 1)

    # 新顺序：旧 2 -> 新 0；新增段 -> 新 1；旧 0 -> 新 2；旧 1 -> 新 3
    r = _reorder(
        client, mid,
        [
            {"source_position": 2},
            {"text": "新原文A"},
            {"source_position": 0},
            {"source_position": 1},
        ],
        expected_revision=2,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 3
    assert body["segment_count"] == 4
    assert body["positions"] == [0, 1, 2, 3]

    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == 3
    segs = draft["segments"]
    assert [s["position"] for s in segs] == [0, 1, 2, 3]

    # 新位置 0 = 旧段 2：版本 1、无译文
    assert segs[0]["source_text"] == "原文2"
    assert segs[0]["source_version"] == 1
    assert segs[0]["status"] == "missing"
    assert segs[0]["translation"] is None

    # 新位置 1 = 新段：版本 1、无译文
    assert segs[1]["source_text"] == "新原文A"
    assert segs[1]["source_version"] == 1
    assert segs[1]["status"] == "missing"

    # 新位置 2 = 旧段 0：版本 2、已确认译文跟随
    assert segs[2]["source_text"] == "原文0-v2"
    assert segs[2]["source_version"] == 2
    assert segs[2]["status"] == "confirmed"
    assert segs[2]["translation"]["target_text"] == "t0-v2"

    # 新位置 3 = 旧段 1：已提交（未确认）译文跟随
    assert segs[3]["source_text"] == "原文1"
    assert segs[3]["status"] == "submitted"
    assert segs[3]["translation"]["target_text"] == "t1"

    # 元数据一致
    meta = client.get(f"/manuscripts/{mid}").json()
    assert meta["revision"] == 3 and meta["segment_count"] == 4

    # 按位置工作的接口以新位置为准
    r = _submit(client, mid, 2, "t0-v2-new", 2)
    assert r.status_code == 200
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["segments"][2]["status"] == "submitted"
    # 旧位置 0 现在是另一段（版本 1），拿版本 2 操作应 409
    assert _submit(client, mid, 0, "x", 2).status_code == 409


def test_reorder_drops_unlisted_segments_completely(client):
    mid = _create(client, 3)
    for i in range(3):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)

    # 只保留旧段 0，其余删除
    r = _reorder(client, mid, [{"source_position": 0}], expected_revision=1)
    assert r.status_code == 200
    assert r.json()["segment_count"] == 1

    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert len(draft["segments"]) == 1
    assert draft["segments"][0]["source_text"] == "原文0"
    assert draft["segments"][0]["status"] == "confirmed"

    # 被删段的位置不再存在 -> 404
    assert client.put(
        f"/manuscripts/{mid}/segments/1/translation",
        json={"text": "x", "source_version": 1},
    ).status_code == 404

    # 子表（译文 / 原文历史）已随段物理删除，无孤儿数据
    import sqlite3

    conn = sqlite3.connect(os.environ["DB_PATH"])
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM segments").fetchall()]
        assert len(ids) == 1
        assert conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0] == 1
    finally:
        conn.close()


def test_reorder_invalid_lists_rejected_without_partial_changes(client):
    mid = _create(client, 2)
    _submit(client, mid, 0, "t0", 1)
    _confirm(client, mid, 0, 1)

    def rejected(segments, expected_revision=1):
        r = _reorder(client, mid, segments, expected_revision)
        assert r.status_code == 422, r.text
        return r

    # 空清单（Pydantic 层）
    assert _reorder(client, mid, [], 1).status_code == 422
    # 同一项同时给引用和原文
    rejected([{"source_position": 0, "text": "x"}])
    # 同一项两者都不给
    rejected([{}])
    # 旧段重复引用
    rejected([{"source_position": 0}, {"source_position": 0}])
    # 引用不存在的旧位置（越界 / 负数）
    rejected([{"source_position": 2}])
    assert _reorder(client, mid, [{"source_position": -1}], 1).status_code == 422
    # 新原文为纯空白
    rejected([{"source_position": 0}, {"text": "   "}])
    # 全部是新段：一篇旧段都没保留
    rejected([{"text": "全新一"}, {"text": "全新二"}])

    # 整次拒绝：修订号、段数、内容、状态全部保持原样
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == 1
    assert [s["source_text"] for s in draft["segments"]] == ["原文0", "原文1"]
    assert draft["segments"][0]["status"] == "confirmed"
    assert client.get(f"/manuscripts/{mid}/publications").json() == []


def test_reorder_stale_revision_conflicts_and_rejects_entirely(client):
    mid = _create(client, 2)

    r = _reorder(
        client, mid, [{"source_position": 1}, {"source_position": 0}], expected_revision=99
    )
    assert r.status_code == 409
    # 即便清单本身无效，修订号过期也应返回 409（过期优先）
    r = _reorder(client, mid, [{"source_position": 0}, {"source_position": 0}], 99)
    assert r.status_code == 409

    # 冲突时没有任何改动
    draft = client.get(f"/manuscripts/{mid}/draft").json()
    assert draft["revision"] == 1
    assert [s["position"] for s in draft["segments"]] == [0, 1]


def test_reorder_bumps_revision_once_even_when_order_unchanged(client):
    mid = _create(client, 2)
    # 即使清单与现状完全一致，编排提交本身也只让修订号 +1
    r = _reorder(
        client, mid, [{"source_position": 0}, {"source_position": 1}], expected_revision=1
    )
    assert r.status_code == 200
    assert r.json()["revision"] == 2

    # 同一次提交不可能被计为多次改版
    r = _reorder(client, mid, [{"source_position": 1}, {"source_position": 0}], 2)
    assert r.status_code == 200
    assert r.json()["revision"] == 3


def test_new_segments_block_publication_until_confirmed(client):
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)

    # 重排：旧 1、旧 0 保留（已确认跟随），再加一段新段
    r = _reorder(
        client, mid,
        [{"source_position": 1}, {"source_position": 0}, {"text": "新增原文"}],
        expected_revision=1,
    )
    assert r.status_code == 200

    # 旧修订号发布 -> 409
    assert _publish(client, mid, 1).status_code == 409
    # 新修订号但新段尚未确认译文 -> 422，阻塞段按新位置
    r = _publish(client, mid, 2)
    assert r.status_code == 422
    assert r.json()["detail"]["blocking_segments"] == [{"position": 2, "source_version": 1}]
    # 不增加版号
    assert client.get(f"/manuscripts/{mid}/publications").json() == []

    # 给新段提交并确认后，整篇（调整后的顺序）才能定版
    assert _submit(client, mid, 2, "t-new", 1).status_code == 200
    assert _confirm(client, mid, 2, 1).status_code == 200
    r = _publish(client, mid, 2)
    assert r.status_code == 201, r.text
    pub = r.json()
    assert pub["revision"] == 2
    assert [s["source_text"] for s in pub["segments"]] == ["原文1", "原文0", "新增原文"]
    assert [s["target_text"] for s in pub["segments"]] == ["t1", "t0", "t-new"]


def test_reorder_after_publication_keeps_old_edition_snapshot(client):
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)
    assert _publish(client, mid, 1).status_code == 201

    # 发布后删除第 1 段并新增一段
    r = _reorder(
        client, mid, [{"text": "新原文"}, {"source_position": 0}], expected_revision=1
    )
    assert r.status_code == 200

    # 旧定版按原版号读取：调整前的两段、revision=1，原样不动
    old = client.get(f"/manuscripts/{mid}/publications/1").json()
    assert old["revision"] == 1
    assert [s["source_text"] for s in old["segments"]] == ["原文0", "原文1"]
    assert [s["target_text"] for s in old["segments"]] == ["t0", "t1"]

    # 新段确认后发布的第 2 版只含调整后的整篇
    _submit(client, mid, 0, "t-new", 1)
    _confirm(client, mid, 0, 1)
    r = _publish(client, mid, 2)
    assert r.status_code == 201
    assert r.json()["edition"] == 2
    assert [s["source_text"] for s in r.json()["segments"]] == ["新原文", "原文0"]


def test_concurrent_reorder_and_publish_never_mix(client):
    """编排与发布交错：定版只能是调整前或调整后的一整篇。

    调整前 rev1 两段均已确认；编排（rev1）删除旧段 1 并在尾部新增未确认段，
    成功后 rev2 不可发布（新段 422）。因此 201 只可能发生在编排之前，
    快照必须是完整的 rev1 旧顺序；任何半调整状态都不允许出现。
    """
    mid = _create(client, 2)
    for i in range(2):
        _submit(client, mid, i, f"t{i}", 1)
        _confirm(client, mid, i, 1)

    barrier = threading.Barrier(2)

    def reorder():
        barrier.wait()
        r = _reorder(
            client, mid,
            [{"source_position": 0}, {"text": "新增原文"}],
            expected_revision=1,
        )
        return r.status_code

    def publish():
        barrier.wait()
        r = _publish(client, mid, 1)
        return r.status_code, r.json() if r.status_code == 201 else None

    with ThreadPoolExecutor(max_workers=2) as ex:
        ro_code, pb = list(ex.map(lambda f: f(), [reorder, publish]))
    pb_code, pb_body = pb

    assert ro_code in (200, 409)
    assert pb_code in (201, 409)

    pubs = client.get(f"/manuscripts/{mid}/publications").json()
    if pb_code == 201:
        # 发布先于编排：只能定格完整的调整前整篇
        assert pb_body["revision"] == 1
        assert [s["source_text"] for s in pb_body["segments"]] == ["原文0", "原文1"]
        assert len(pubs) == 1
        # 发布不推进修订号，编排随后仍可成功
        assert ro_code == 200
    else:
        # 编排先提交：rev1 发布冲突，rev2 又有未确认新段，故无定版
        assert ro_code == 200
        assert pubs == []

    # 编排一旦完成，工作稿只可能呈现调整后的完整形态
    r = _publish(client, mid, 2)
    assert r.status_code == 422
    assert r.json()["detail"]["blocking_segments"] == [{"position": 1, "source_version": 1}]
