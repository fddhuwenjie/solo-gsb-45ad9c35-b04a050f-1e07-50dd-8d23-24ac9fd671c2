# 双语文稿定版服务（Python + FastAPI + SQLite）

把有序的原文段与一种人工译文维护为可回看的双语**定版**。支持原文改写、
带版本依据的译文提交 / 确认、失效待复核、乐观修订号校验、整篇段落编排
（重排 / 删除 / 新增），以及原子发布不可变的整篇双语快照。

## 领域模型与规则

- **稿件（manuscript）**：拥有一个工作稿修订号 `revision`，初始为 1，
  在某段原文内容**真正改变**、或一次**段落编排提交成功**时 +1
  （一次编排只加一次，与改动幅度无关）。
- **原文段（segment）**：按 `position` 有序排列（从 0 起连续编号），
  每段有独立的 `source_version`（从 1 起）。
- **译文（translation）**：提交时必须注明 `source_version`（所依据的
  原文版本）。状态为 `submitted` / `confirmed`；段状态额外有
  `missing` / `stale`（待复核）。
- 依据**过期原文版本**的译文提交或确认一律 `409 Conflict` 拒绝。
- 原文段改版时，**仅该段**已确认译文翻转为 `stale`（待复核），
  其他段不受影响；需要基于新原文版本重新提交、确认。
- **段落编排**（`PUT /manuscripts/{id}/segments/reorder`）一次提交
  整篇工作稿的新有序清单，每项二选一：
  - `{"source_position": p}`：引用**调整前**位置 `p` 的旧段，成功后该段的
    原文版本、译文与确认状态整体跟随到新位置；
  - `{"text": "..."}`：在该位置插入新原文，新段从 `source_version=1`
    开始且没有译文（段状态 `missing`）。

  校验规则（任一不满足整次拒绝，`422`，不留任何部分改动）：

  - 清单非空，且**至少保留一段旧段**（不能把整篇都换成新段）；
  - 旧段位置不能重复引用，且必须存在于调整前的工作稿中；
  - 每项必须且只能给出 `source_position` 与 `text` 之一，新原文不能为空。

  `expected_revision` 与当前修订号不一致时 `409` 拒绝。成功后位置从 0
  连续重编号，未列出的旧段（连同其译文与原文历史）从工作稿移除，
  修订号仅 +1。此后按位置改写 / 提交 / 确认译文及工作稿查询全部以
  **新位置**为准；新段在拥有匹配当前版本的已确认译文之前会阻塞发布。
- **发布定版**必须携带 `expected_revision`：
  - 与当前修订号不一致 → `409`（修订冲突）；
  - 存在未准备好的段（无匹配当前原文版本的已确认译文）→ `422`，
    响应体列出阻塞段；两种失败都**不增加版号**。
  - 全部满足时，在单个立即写事务中原子生成新定版，内容是发布瞬间的
    整篇双语文本 JSON 快照。发布、原文改写与段落编排通过 SQLite 写锁
    互斥，编排与发布交错时定版只能包含**调整前或调整后的一整篇**，
    快照不可能混入不同工作稿修订的数据。
- **定版不可变**：按版号读取的永远是发布时的快照，后续改写 / 复核 /
  编排都不会改变它。

## 单条命令启动（Docker Compose）

```bash
docker compose up --build
```

服务监听在本机 **http://localhost:8000**（容器内 8000，SQLite 数据持久化
在命名卷 `bilingual-data` 的 `/data/bilingual.db`）。

- 健康检查：http://localhost:8000/health
- 交互式 API 文档（Swagger UI）：http://localhost:8000/docs

停止并保留数据：`docker compose down`；连数据卷一起删除：
`docker compose down -v`。

## 本地无容器运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DB_PATH=./bilingual.db uvicorn app.main:app --reload --port 8000
```

## 可执行的 HTTP 调用示例

以下示例可直接复制到终端顺序执行（依赖 [jq](https://stedolan.github.io/jq/)
提取 id；不使用 jq 时可从响应里手工读取 `manuscript_id`）。

### 1. 创建稿件（录入有序原文段）

```bash
curl -sS -X POST http://localhost:8000/manuscripts \
  -H 'Content-Type: application/json' \
  -d '{"title": "示例", "segments": ["第一段原文。", "第二段原文。"]}'
# => {"manuscript_id":1,"revision":1,"segment_count":2}

MID=1
```

### 2. 查询工作稿（各段版本与状态）

```bash
curl -sS http://localhost:8000/manuscripts/$MID/draft | jq .
# 初始每段 status 为 "missing"，source_version 为 1
```

### 3. 提交译文（注明所依据的原文版本）

```bash
curl -sS -X PUT http://localhost:8000/manuscripts/$MID/segments/0/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "Source paragraph one.", "source_version": 1}'
# => {"status":"submitted","source_version":1,...}
```

### 4. 确认译文

```bash
curl -sS -X POST http://localhost:8000/manuscripts/$MID/segments/0/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}'
# => {"status":"confirmed",...}

curl -sS -X PUT http://localhost:8000/manuscripts/$MID/segments/1/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "Source paragraph two.", "source_version": 1}' >/dev/null
curl -sS -X POST http://localhost:8000/manuscripts/$MID/segments/1/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}'
```

### 5. 发布定版（携带预期工作稿修订号）

```bash
curl -sS -X POST http://localhost:8000/manuscripts/$MID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}' | jq .
# => 201，edition=1，包含整篇 segments 双语内容
```

### 6. 改写原文：旧确认译文失效待复核，仅影响该段

```bash
curl -sS -X PUT http://localhost:8000/manuscripts/$MID/segments/0/source \
  -H 'Content-Type: application/json' \
  -d '{"text": "第一段原文（修订）。", "expected_revision": 1}'
# => {"source_version":2,"revision":2,"changed":true}

curl -sS http://localhost:8000/manuscripts/$MID/draft | jq '.segments[] | {position, source_version, status}'
# 第 0 段: status="stale"（旧译文在 stale_translation 中待复核）
# 第 1 段: 仍是 "confirmed"，不受影响
```

此时再发布：

```bash
# 用旧修订号 -> 409 修订冲突
curl -i -X POST http://localhost:8000/manuscripts/$MID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}'

# 用新修订号但第 0 段尚未重新确认 -> 422，列出阻塞段，不增加版号
curl -i -X POST http://localhost:8000/manuscripts/$MID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}'
# 422 body: {"detail":{"reason":"segments_not_confirmed",
#   "blocking_segments":[{"position":0,"source_version":2}]}}

# 依据过期版本提交/确认 -> 409
curl -i -X POST http://localhost:8000/manuscripts/$MID/segments/0/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}'
```

补齐第 0 段译文并确认后，再发布第二版：

```bash
curl -sS -X PUT http://localhost:8000/manuscripts/$MID/segments/0/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "First paragraph (revised).", "source_version": 2}' >/dev/null
curl -sS -X POST http://localhost:8000/manuscripts/$MID/segments/0/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 2}' >/dev/null

curl -sS -X POST http://localhost:8000/manuscripts/$MID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}' | jq '.edition, .revision'
# => 2, 2
```

### 7. 按版号回看旧定版（永不改变）

```bash
curl -sS http://localhost:8000/manuscripts/$MID/publications/1 | jq .
# 仍是第 1 版发布时的原文与译文，revision=1

curl -sS http://localhost:8000/manuscripts/$MID/publications | jq '.[].edition'
# 列出全部定版：1, 2
```

### 8. 段落编排：重排 / 删除 / 新增（一次提交整篇清单）

另建一篇三段稿件用于演示：

```bash
RID=$(curl -sS -X POST http://localhost:8000/manuscripts \
  -H 'Content-Type: application/json' \
  -d '{"title": "编排示例", "segments": ["第一段。", "第二段。", "第三段。"]}' | jq .manuscript_id)

# 先给旧第 0 段准备一条已确认译文（验证它会跟随到新位置）
curl -sS -X PUT http://localhost:8000/manuscripts/$RID/segments/0/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "First paragraph.", "source_version": 1}' >/dev/null
curl -sS -X POST http://localhost:8000/manuscripts/$RID/segments/0/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}' >/dev/null
```

提交新有序清单：旧第 2 段移到最前，中间插入一段新原文，旧第 0 段放到最后，
旧第 1 段未被引用因而从工作稿删除：

```bash
curl -sS -X PUT http://localhost:8000/manuscripts/$RID/segments/reorder \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "segments": [
        {"source_position": 2},
        {"text": "新增的第四段。"},
        {"source_position": 0}
      ]}' | jq .
# => {"manuscript_id":2,"revision":2,"segment_count":3,"positions":[0,1,2]}
```

位置已从 0 连续重编号；保留段的原文版本、译文与确认状态跟随原段，
新段版本为 1 且缺少译文：

```bash
curl -sS http://localhost:8000/manuscripts/$RID/draft | jq '.segments[] | {position, source_text, source_version, status}'
# position 0: "第三段。"        source_version=1 status="missing"   （旧第 2 段）
# position 1: "新增的第四段。"  source_version=1 status="missing"   （新段，无译文）
# position 2: "第一段。"        source_version=1 status="confirmed" （旧第 0 段，译文跟随）
```

此后按位置的接口都以**新位置**工作；新段未确认译文时发布会被阻塞：

```bash
# 旧修订号 -> 409
curl -i -X POST http://localhost:8000/manuscripts/$RID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 1}'

# 新修订号，但新位置 0、1 两段尚无已确认译文 -> 422 阻塞
curl -i -X POST http://localhost:8000/manuscripts/$RID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}'
# 422 body: {"detail":{"reason":"segments_not_confirmed",
#   "blocking_segments":[{"position":0,"source_version":1},
#                        {"position":1,"source_version":1}]}}
```

无效清单一律 `422` 整次拒绝（清单不会被部分应用）；修订号过期为 `409`：

```bash
# 旧段重复引用
curl -i -X PUT http://localhost:8000/manuscripts/$RID/segments/reorder \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2, "segments": [{"source_position":0},{"source_position":0}]}'

# 引用了调整前不存在的位置
curl -i -X PUT http://localhost:8000/manuscripts/$RID/segments/reorder \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2, "segments": [{"source_position":9}]}'

# 一篇旧段都没保留（整篇换成新段）
curl -i -X PUT http://localhost:8000/manuscripts/$RID/segments/reorder \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2, "segments": [{"text":"全新 A"},{"text":"全新 B"}]}'

# 同一项同时给了引用与原文（二者必须二选一）
curl -i -X PUT http://localhost:8000/manuscripts/$RID/segments/reorder \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 2, "segments": [{"source_position":0,"text":"x"}]}'

# 修订号过期
curl -i -X PUT http://localhost:8000/manuscripts/$RID/segments/reorder \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "segments": [{"source_position":0}]}'
# => 409
```

补齐新位置 0、1 两段的译文并确认后，发布的就是**调整后的一整篇**；
此前已存在的旧定版按原版号读取仍是编排前的内容，不受影响：

```bash
curl -sS -X PUT http://localhost:8000/manuscripts/$RID/segments/0/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "Third paragraph.", "source_version": 1}' >/dev/null
curl -sS -X POST http://localhost:8000/manuscripts/$RID/segments/0/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}' >/dev/null
curl -sS -X PUT http://localhost:8000/manuscripts/$RID/segments/1/translation \
  -H 'Content-Type: application/json' \
  -d '{"text": "Newly added fourth paragraph.", "source_version": 1}' >/dev/null
curl -sS -X POST http://localhost:8000/manuscripts/$RID/segments/1/confirmation \
  -H 'Content-Type: application/json' -d '{"source_version": 1}' >/dev/null

curl -sS -X POST http://localhost:8000/manuscripts/$RID/publications \
  -H 'Content-Type: application/json' -d '{"expected_revision": 2}' | jq .
# => 201，segments 顺序为：第三段。 / 新增的第四段。 / 第一段。
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/manuscripts` | 创建稿件并录入有序原文段 |
| GET | `/manuscripts/{id}` | 稿件元数据（含当前修订号） |
| GET | `/manuscripts/{id}/draft` | 工作稿：各段原文版本、译文、状态 |
| PUT | `/manuscripts/{id}/segments/{p}/source` | 改写原文（带 `expected_revision`） |
| PUT | `/manuscripts/{id}/segments/reorder` | 整篇段落编排：重排 / 删除 / 新增（带 `expected_revision` 与有序清单） |
| PUT | `/manuscripts/{id}/segments/{p}/translation` | 提交/改写译文（带 `source_version`） |
| POST | `/manuscripts/{id}/segments/{p}/confirmation` | 确认译文（带 `source_version`） |
| POST | `/manuscripts/{id}/publications` | 原子发布定版（带 `expected_revision`） |
| GET | `/manuscripts/{id}/publications` | 定版列表 |
| GET | `/manuscripts/{id}/publications/{edition}` | 按版号回看定版快照 |
| GET | `/health` | 健康检查 |

主要状态码：`200/201` 成功；`404` 稿件/段/定版不存在；`409` 修订冲突或
依据过期原文版本；`422` 请求体校验失败，或发布时存在阻塞段。

## 测试

```bash
pip install pytest httpx
PYTHONPATH=. python -m pytest tests -q
```

测试覆盖状态流转、过期版本拒绝、改版仅影响单段、发布的冲突与阻塞段、
失败不增版号、定版快照不可变，以及改写与发布并发下快照不跨修订拼装
（含多轮并发压测）。段落编排覆盖保留段版本 / 译文 / 确认状态随位置跟随、
未列出旧段连同子表删除、新段从版本 1 起阻塞发布、无效清单与过期修订号
整次拒绝不留部分改动、旧定版快照不受编排影响，以及编排与发布并发时
定版只能是调整前或调整后的一整篇。
