# facility-repair-agent

企业设施报修 AI Agent。用户通过聊天（文字/图片）描述故障，系统自动收集报修信息、可选分析现场图片、通过 RAG 标准化故障类型，并生成可提交的结构化工单 JSON。

## 技术栈

- **后端**：Python 3.11 + FastAPI + SSE（`sse-starlette`）
- **Agent 编排**：LangGraph `StateGraph`，单轮消息图执行；Session 由外部存储维护
- **LLM/VLM**：Qwen3.5 Omni（DashScope OpenAI 兼容接口），`openai.AsyncOpenAI + httpx`
- **RAG**：ChromaDB + `BAAI/bge-large-zh-v1.5`，历史工单向量检索
- **前端**：TypeScript + Lit Web Component + Vite，SSE 流式聊天
- **图片**：默认本地存储到 `backend/data/uploads`，对外挂载 `/uploads`
- **持久化**：Memory（本地/测试）或 Redis（生产/多 worker）

## 项目结构

```text
backend/
  app/
    api/v1/              # chat / upload / ticket API
    agent/               # LangGraph 状态机、节点、状态、结构化 schema
    models/              # API Pydantic 请求/响应模型
    services/            # LLM、RAG、存储、工单号、图片存储服务
    config.py            # .env 配置入口
    main.py              # FastAPI app、lifespan、CORS、静态上传目录
  tests/
    agent/               # 图集成测试、LLM 调用预算回归测试
    services/            # Redis/Session 序列化/RAG 相关测试
frontend/
  src/
    components/          # Lit 组件：聊天面板、消息、输入栏、FAB
    services/            # API、SSE parser、图片压缩、语音
    stores/              # ChatStore：前端会话与 SSE 事件处理
    repair-agent.ts      # <repair-agent> Web Component 入口与自动挂载
docs/                    # 工作流、LangGraph 架构、字段规则、维护计划
```

## 后端关键文件

| 路径 | 作用 |
|------|------|
| `agent/state.py` | `AgentState`、`TicketDraft`、`Session`，含完整 `serialize()/deserialize()`；Session 缓存 `image_analysis`、`pending_clarification`、`pending_conflict`、`ticket` |
| `agent/schemas.py` | Pydantic v2 结构化输出契约：`ImageAnalysis`、`TextExtraction`、`ConfirmationIntent`、`ErrorResult` |
| `agent/graph_state.py` | LangGraph `GraphState` TypedDict；`events` 使用 `operator.add` reducer |
| `agent/graph.py` | 构建并编译 LangGraph；`process_message()` 是后端聊天入口，统一追加 `done` 事件并记录 LLM 调用计数 |
| `agent/edges.py` | 节点名常量 + 条件路由函数 |
| `agent/nodes.py` | 节点聚合导出层，兼容原导入路径；实际实现拆到 collection/confirmation/terminal 节点文件 |
| `agent/collection_nodes.py` | 采集阶段节点：入口路由、文本/图片并行提取、缺字段追问、可选图片等待 |
| `agent/confirmation_nodes.py` | 确认/预览阶段节点：RAG 确认卡片、确认生成预览、修改字段、重新确认 |
| `agent/conflict_nodes.py` | 图文或字段冲突挂起与用户选择解析；用户可选择以文字或图片为准 |
| `agent/terminal_nodes.py` | 转人工、已提交、已完成、finalize 终态节点 |
| `agent/node_utils.py` | SSE 事件构造、图片分析缓存、错误降级、确认卡片事件等节点工具 |
| `agent/draft_ops.py` | 纯函数字段操作：`apply_extraction`、`merge_extraction`、位置推断、RAG 字段清理等 |
| `agent/prompts.py` | Prompt 模板与 prompt 组装函数 |
| `agent/templates.py` | 固定文本模板（缺字段追问、确认摘要、预览更新、分句输出），不消耗 LLM |
| `agent/ticket_builder.py` | 组装工单 JSON；通过 `TicketCounter` 获取全局递增 `repair_no` |
| `services/llm_client.py` | 统一 LLM 客户端：单例 `AsyncOpenAI`、`trust_env=False`、timeout、tenacity 重试、JSON 修复重试、ContextVar 调用计数 |
| `services/llm.py` | 高层 LLM/VLM 业务函数：文本提取、图片分析、确认意图、流式追问、自然语言时间解析 |
| `services/rag.py` | 故障描述标准化 + ChromaDB 检索；图片摘要仅在相关且未被用户忽略时拼入查询 |
| `services/session_store.py` | `SessionStore` 抽象 + Memory/Redis 实现；Redis 用 `SETEX` 保存会话、`SET NX EX` 锁并发 |
| `services/ticket_counter.py` | `TicketCounter` 抽象 + Memory/Redis 实现；Redis 用 `SETNX + INCR` 保证跨进程唯一 |
| `services/storage.py` | 图片上传本地存储与读取；限制内容类型和 10MB 大小（上传接口负责校验） |
| `api/v1/chat.py` | `POST /api/v1/chat/init`、`POST /api/v1/chat/message`；消息接口返回 SSE |
| `api/v1/upload.py` | `POST /api/v1/upload/image`；上传图片后返回 `image_url` |
| `api/v1/ticket.py` | `POST /api/v1/ticket/submit`；仅 `PREVIEW_READY` 可提交，提交后状态为 `SUBMITTED` |

## 前端关键文件

| 路径 | 作用 |
|------|------|
| `src/repair-agent.ts` | 注册 `<repair-agent>`，读取 `data-config`，支持脚本自动挂载 |
| `src/stores/chat-store.ts` | 前端核心状态；初始化 session、发送文本/图片、解析 SSE、派发业务事件、提交工单 |
| `src/types.ts` | Widget 配置、消息、确认卡片、SSE 事件、API 响应类型 |
| `src/services/api.ts` | 后端 API 调用封装 |
| `src/services/sse-parser.ts` | SSE 流解析 |
| `src/services/image-compress.ts` | 上传前图片压缩 |
| `src/components/chat-panel.ts` | 聊天面板与工单预览交互 |
| `src/components/message-bubble.ts` | 文本、图片、确认卡片消息渲染 |
| `src/components/input-bar.ts` | 输入栏、上传图片、语音入口 |

前端对外事件：

- `onRepairTicketGenerated`：收到 `ticket_ready`，携带服务端生成的工单快照
- `onRequestHumanService`：收到 `human_service`，携带 `session_id`、`reason`、`partial_ticket`

## Agent 状态机（LangGraph 图结构）

```text
entry_router（按 session.state 分派；GREETING 首次转 COLLECTING）
    ├─ COLLECTING → collect_extract
    │   ├─ _error → finalize
    │   ├─ needs_human → escalated → finalize
    │   ├─ clarification/conflict → finalize（记录 pending_clarification / pending_conflict）
    │   ├─ missing → collect_decide
    │   │   ├─ stalled → escalated → finalize
    │   │   └─ normal → stream_reply → finalize
    │   └─ complete → rag_and_confirm → finalize
    ├─ WAITING_IMAGE → wait_image
    │   ├─ proceed → rag_and_confirm → finalize
    │   └─ retry/clarification/conflict → finalize
    ├─ CONFIRMING → confirming
    │   ├─ confirmed → build_ticket → PREVIEW_READY → finalize
    │   ├─ restart → 清空 draft → COLLECTING → finalize
    │   ├─ modify + need_rerag → rag_and_confirm → finalize
    │   ├─ modify + no_rerag → re_confirm → finalize
    │   └─ unclear/conflict → finalize
    ├─ PREVIEW_READY → preview_edit
    │   ├─ restart → 清空 draft → COLLECTING → finalize
    │   ├─ description/image changed → rag_and_confirm → finalize
    │   ├─ other fields changed → build_ticket → ticket_ready → finalize
    │   └─ unclear/conflict → finalize
    ├─ ESCALATED → escalated → finalize
    ├─ SUBMITTED → submitted → finalize
    └─ COMPLETED → completed → finalize
```

**Session 状态流转**：

```text
GREETING → COLLECTING → WAITING_IMAGE → CONFIRMING → PREVIEW_READY → SUBMITTED
                                ↓（任意阶段）
                           ESCALATED（转人工）

COMPLETED 为外部系统回调预留状态。
```

关键语义：

- `CONFIRMING` 展示聊天内确认卡片；用户确认后才生成 `session.ticket` 并进入 `PREVIEW_READY`。
- `PREVIEW_READY` 表示工单预览已生成，等待前端 `POST /api/v1/ticket/submit` 或继续修改。
- `SUBMITTED` 由 `/ticket/submit` 设置；当前只更新会话状态并返回成功，真实下游工单系统调用仍是 TODO。
- `done` SSE 事件只由 `process_message()` 末尾统一发送，节点不要重复发送。

## GraphState 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `session` | `Session` | 会话对象，节点直接修改 |
| `user_message` | `str` | 本轮用户输入 |
| `image_url` | `str | None` | 本轮图片 URL |
| `events` | `list[dict]` | SSE 事件累积通道，`operator.add` 合并 |
| `_extraction` | `dict` | `collect_extract` 提取/错误/冲突结果，供路由判断 |
| `_proceed_to_rag` | `bool` | `wait_image` 后是否进入 RAG |
| `_intent` | `str` | `confirming` 后意图：`confirmed/restart/modify/unclear` 等 |
| `_need_rerag` | `bool` | 修改后是否需要重新 RAG |

## 必填字段与草稿字段

必填字段：

- `description`：故障描述
- `estate`：楼盘/项目
- `building`：楼栋
- `floor`：楼层
- `visit_time`：期望上门时间

可选但会保留/推断：

- `area`：区域，如大堂、卫生间、停车场
- `room`：房间/点位，如 301、会议室
- `image_urls`：图片附件
- RAG 字段：`normalized_description`、`fault_type_code`、`fault_type_name`、`repair_priority_rag`、`repair_type`

位置规则：`infer_location_from_area_or_room()` 会基于 `area/room` 辅助推断 `building/floor`；修改 `area/room` 时需注意清理依赖位置字段，避免旧楼栋/楼层残留。

## SSE 事件契约

前端 `SSEEvent.type` 当前包含：

| 类型 | 说明 |
|------|------|
| `text_delta` | 机器人文本增量 |
| `state_update` | Agent 状态和已收集字段更新 |
| `draft_confirm` | 确认卡片草稿；进入 `CONFIRMING` 时展示 |
| `ticket_ready` | 工单预览已生成；前端保存 `currentTicket` 并派发 `onRepairTicketGenerated` |
| `human_service` | 转人工；前端派发 `onRequestHumanService` |
| `error` | 错误，如 `BUSY`、`INTERNAL_ERROR`、`STREAM_ERROR` |
| `done` | 本轮 SSE 结束 |

## LLM 调用架构

- **统一入口**：所有模型调用通过 `services/llm_client.py`。
- **结构化输出**：`structured_call(schema, ...)` 使用 `response_format={"type":"json_object"}`，先校验 Pydantic v2；失败后给 LLM 1 次 JSON 修复机会；仍失败返回 `ErrorResult`。
- **多模态图片分析**：`analyze_image()` 一次 VLM 调用返回 `ImageAnalysis`：
  - `visual_description`：给用户看的 2-3 句图片描述
  - `visual_fault_summary`：给 RAG 用的 ≤20 字故障摘要
  - `visual_fields`：图片中观察到的位置/描述字段
- **图片缓存**：`Session.image_analysis` 按当前图片结果缓存，`maybe_analyze_image()` 避免重复看图。
- **确认关键词快速路径**：`classify_confirmation_intent()` 对“是的/好的/确认/重新来”等先做规则匹配，命中不调用 LLM。
- **模板化固定文本**：确认卡片、预览更新、缺字段兜底追问走 `templates.py`，不消耗 LLM。
- **图文冲突单一策略**：字段合并统一在 `draft_ops.merge_extraction()`；若发现冲突，通过 `pending_conflict` 追问“以文字还是图片为准”。
- **治理**：`httpx.Timeout(settings.llm_timeout_seconds, connect=5.0)`、`trust_env=False` 绕开 Windows 系统代理、tenacity 最多 `settings.llm_max_retries` 次指数退避。
- **调用计数**：`ContextVar Counter` 每轮 `process_message()` 重置，结束时日志输出 `[llm_calls] session=... total=... detail=...`。

## RAG 规则

- RAG collection 名称：`historical_tickets`。
- `main.py` lifespan 会预热 Embedding 模型和 ChromaDB collection；失败时服务仍启动，但 RAG 会跳过。
- `search_fault(description, visual_fault_summary, ignore_image=False)`：
  1. 判断图片摘要和用户描述是否相关；不相关则图片仅作为附件保存，不拼入查询。
  2. 调用 LLM `rag_normalize` 将查询标准化为“物理实体 + 故障现象”。
  3. Embedding 后检索 Top 3；分数 `<0.3` 返回 `None`。
  4. 命中后写入故障类型、维修优先级、维修类型和规范化描述。
- 用户明确“以我的描述为准”时，设置 `user_confirmed_description_priority=True`，RAG 传 `ignore_image=True`。
- RAG 返回 `None` 时工单仍可生成，故障类型默认为 `000/待分类`，优先级默认为 `MEDIUM`。

## 持久化层

本系统定位为**报修信息收集与确认层**。最终工单档案、审核、修改应在下游业务系统完成；本系统只持有会话草稿和预览快照。

持久化层目标：

1. 会话状态跨进程/重启可恢复。
2. `repair_no` 跨进程不重复，可作为幂等键/链路追踪号。
3. 工单本身不作为长期档案持久化；`session.ticket` 随 session TTL 过期。

### 后端切换

通过 `.env` 的 `storage_backend` 选择：

| 模式 | 适用场景 | 配置 |
|------|---------|------|
| `memory` | 单进程开发/测试 | `storage_backend=memory` |
| `redis` | 生产、多 worker、滚动发布 | `storage_backend=redis` + `redis_url=redis://localhost:6379/0` |

`main.py` 的 `lifespan` 会按配置初始化 `SessionStore` 和 `TicketCounter`。

### 并发控制与一致性

- Memory：`dict + asyncio.Lock`，同 session 请求串行。
- Redis：会话用 `SETEX` 存 JSON；锁用 `SET NX EX`，非阻塞获取。
- 锁已占用时 `/chat/message` 直接返回 SSE error：`{"type":"error","code":"BUSY"}`。
- 节点执行期间不写 Redis；SSE 流结束后在持锁状态下 `store.save(session)` 一次写回。
- 崩溃代价：最坏丢失本轮对话；旧会话仍可从上次保存点恢复。
- Redis 反序列化失败会删除旧 key，并让上层重新初始化会话。

## 配置项

```bash
# Qwen / DashScope OpenAI compatible API
qwen_api_key=...
qwen_base_url=https://dashscope.aliyuncs.com/compatible-mode/v1
qwen_model=qwen3.5-omni-flash

# RAG
embedding_model_path=BAAI/bge-large-zh-v1.5
chroma_persist_dir=backend/data/chromadb

# 上传与 CORS
use_local_storage=true
local_upload_dir=backend/data/uploads
allowed_origins=*

# Session
session_ttl_seconds=1800
max_stall_count=3

# 持久化
storage_backend=memory          # memory | redis
redis_url=redis://localhost:6379/0
redis_session_prefix=frap:session:
redis_lock_prefix=frap:lock:
redis_lock_ttl_seconds=60
repair_no_seed=1726198

# LLM 调用治理
llm_timeout_seconds=15.0
llm_max_retries=3
```

## 关键约定

- `needs_human` 触发词必须包含“人工”或“客服”；“联系人来维修”等正常报修不能触发转人工。
- 图片是可选项；字段齐全后进入确认卡片，用户可上传图片或回复“跳过/不用/没有/算了/不需要/skip”。
- 用户文字描述是故障事实主来源；图片用于附件、位置补充、相关时辅助 RAG。
- `pending_clarification` 保存上一轮澄清问题，下一轮传给 `extract_text_fields()` 后清空。
- `pending_conflict` 保存图文/字段冲突；用户需回复类似“按文字/按图片”。
- `user_confirmed_description_priority` 在 `COLLECTING`、`WAITING_IMAGE`、`CONFIRMING`、`PREVIEW_READY` 均可被 LLM 提取并写回 Session。
- `confirming` 的 modify 分支在节点内完成字段更新，不回跳 `collect_extract`。
- `description` 或有效新图变更必须 `clear_rag_fields()` 并重新走 `rag_and_confirm`；其他字段变更走 `re_confirm` 或直接重建 ticket，跳过 RAG。
- `restart` 分支清空 `draft/pending_* / image_analysis` 并回到 `COLLECTING`；“重新来”这句话不进入字段提取。
- `ticket_ready` 中的 `repair_no` 每次 `build_ticket()` 都会递增；避免在无必要场景反复重建预览。
- `uvicorn` 开发启动建议加 `--reload-dir app`，避免 `data/uploads/` 写入触发热重载。

## LLM 调用预算（回归测试锁定）

`backend/tests/agent/test_call_budget.py` 锁定关键路径上限。修改节点/LLM 链路时必须保持预算不回退。

| 路径 | 调用次数上限 | 说明 |
|------|--------------|------|
| 纯文本首轮采集 | `text_extract=1 + reply_stream=1` = **2** | 缺失字段追问 |
| 带图首轮采集（全字段） | `text_extract=1 + image_analysis=1 + rag_normalize=1` = **3** | 文本/图片并行 + RAG 标准化 |
| 用户确认（关键词命中） | **0** | “是的/好的/确认/生成预览”等快速路径 |
| 用户确认（关键词未命中） | `confirmation_intent=1` = **1** | LLM 意图分类 |
| 修改时间 | `confirmation_intent=1 + resolve_visit_time=1` = **2** | 不触发 RAG |
| 修改描述（无新图） | `confirmation_intent=1 + rag_normalize=1` = **2** | 复用已缓存图片分析 |
| 修改描述（带新图） | `confirmation_intent=1 + image_analysis=1 + rag_normalize=1` = **3** | 新图重新分析 + RAG |

## 测试与验证

后端：

```powershell
cd backend
uv run pytest tests\agent tests\services\test_session_serialization.py tests\services\test_rag_relevance.py
uv run pytest tests\services\test_redis_store.py
```

前端：

```powershell
cd frontend
npm run build
```

常用启动：

```powershell
cd backend
uv run uvicorn app.main:app --reload --reload-dir app

cd frontend
npm run dev
```

## 维护注意事项

- 写代码时优先保持节点职责单一：采集逻辑放 `collection_nodes.py`，确认/预览逻辑放 `confirmation_nodes.py`，终态放 `terminal_nodes.py`，通用小工具放 `node_utils.py` 或 `draft_ops.py`。
- 新增 LLM 调用必须通过 `llm_client.py`，并在调用预算测试中说明/锁定。
- 新增结构化 LLM 结果要先在 `schemas.py` 建 Pydantic 模型，再接入 `structured_call()`。
- 新增 Session/Draft 字段必须同步更新 `serialize()/deserialize()`、前端类型和相关测试。
- 修改 SSE 事件结构时同步更新 `frontend/src/types.ts` 与 `ChatStore._handleSSE()`。
- 修改位置字段语义时参考 `docs/plan-area-refactor.md`。
- 修改工作流或图结构时同步更新 `docs/workflow-analysis.md`、`docs/langgraph-refactor-summary.md`。
