# facility-repair-agent

企业设施报修 AI Agent——用户通过聊天（文字/图片）描述故障，系统自动收集信息并生成结构化工单 JSON。

## 技术栈

- **后端**：Python FastAPI + Qwen3（DashScope OpenAI 兼容接口）
- **Agent 编排**：LangGraph StateGraph
- **RAG**：ChromaDB + BAAI/bge-large-zh-v1.5
- **前端**：TypeScript Lit Web Component，SSE 流式输出
- **图片**：本地存储（默认）或 MinIO

## 后端结构 (`backend/app/`)

| 路径 | 作用 |
|------|------|
| `agent/state.py` | AgentState 枚举、TicketDraft、Session（含 serialize/deserialize） |
| `agent/graph_state.py` | LangGraph GraphState TypedDict |
| `agent/graph.py` | 图构建、compiled_graph、process_message() 入口 |
| `agent/nodes.py` | 14 个节点函数实现 |
| `agent/edges.py` | 节点名常量 + 条件路由函数 |
| `agent/draft_ops.py` | 纯函数工具（apply_extraction、infer_location 等） |
| `agent/prompts.py` | 所有 Prompt 模板 |
| `agent/ticket_builder.py` | 组装工单 JSON（async，通过 TicketCounter 获取 repair_no） |
| `services/llm.py` | Qwen API 封装 |
| `services/rag.py` | ChromaDB 检索 |
| `services/session_store.py` | 会话存储抽象接口 + Memory/Redis 两套实现 |
| `services/ticket_counter.py` | 工单号计数器抽象接口 + Memory/Redis 两套实现 |
| `api/v1/chat.py` | /chat/init、/chat/message |
| `api/v1/upload.py` | /upload/image |
| `api/v1/ticket.py` | /ticket/submit |

## Agent 状态机（LangGraph 图结构）

```
entry_router（按 session.state 分派）
    ├─ COLLECTING → collect_extract
    │   ├─ _error → finalize
    │   ├─ needs_human → escalated → finalize
    │   ├─ clarification → finalize（记录 pending_clarification）
    │   └─ missing → collect_decide
    │       ├─ stalled → escalated → finalize
    │       └─ normal → stream_reply → finalize
    │   ├─ no_image → ask_image → finalize
    │   └─ complete → rag_and_confirm → finalize
    ├─ WAITING_IMAGE → wait_image
    │   ├─ proceed → rag_and_confirm → finalize
    │   └─ retry → finalize
    ├─ CONFIRMING → confirming
    │   ├─ confirmed → finalize
    │   ├─ restart → finalize（清空 draft，等待下一轮）
    │   ├─ modify + need_rerag → rag_and_confirm → finalize
    │   ├─ modify + no_rerag → re_confirm → finalize
    │   └─ unclear → finalize（追问用户）
    ├─ PREVIEW_READY → preview_edit
    │   ├─ need_rerag → rag_and_confirm → finalize
    │   └─ no_rerag → finalize
    ├─ ESCALATED → escalated → finalize
    ├─ SUBMITTED → submitted → finalize
    └─ COMPLETED → completed → finalize
```

**Session 状态流转**：
```
GREETING → COLLECTING → WAITING_IMAGE → CONFIRMING → PREVIEW_READY → SUBMITTED
                                ↓（任何阶段）
                           ESCALATED（转人工）
```

- CONFIRMING：用户确认后生成工单 → PREVIEW_READY
- PREVIEW_READY：等待用户提交或修改字段（description/image 变更重新 RAG；其他字段变更走 re_confirm 跳过 RAG）
- SUBMITTED：前端调用 POST /ticket/submit 后进入
- COMPLETED：外部系统回调更新（预留）

## GraphState 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `session` | Session | 会话对象，节点直接修改 |
| `user_message` | str | 本轮用户输入 |
| `image_url` | str \| None | 本轮图片 URL |
| `events` | list[dict] | SSE 事件累积（operator.add reducer） |
| `_extraction` | dict | collect_extract 提取结果，供路由判断 |
| `_proceed_to_rag` | bool | wait_image 后是否进入 RAG |
| `_intent` | str | confirming 后意图（confirmed/restart/modify/unclear） |
| `_need_rerag` | bool | confirming/preview_edit 后是否重新 RAG |

## 必填字段

`description`、`estate`、`building`、`floor`、`visit_time`

## 关键约定

- `needs_human` 触发词必须包含"人工"或"客服"；"联系人来维修"等是正常报修，不触发
- RAG 检索依赖 ChromaDB 预先入库，返回 None 时工单仍可生成
- 跳过图片关键词：跳过/不用/没有/算了/不需要/skip
- LLM 调用均设 `enable_thinking: false`
- LLM 失败返回 `{"_error": "llm_call_failed"}`，调用方检测后向用户提示"系统繁忙"
- `generate_reply_stream` 只传最近 10 条 history，避免 token 浪费
- `generate_confirmation_stream` 不依赖 history，纯基于 draft 生成
- 不使用 LangGraph Checkpointer；Session 状态由外部内存字典管理，图仅处理单次消息
- uvicorn 启动建议加 `--reload-dir app`，避免 data/uploads/ 写入触发热重载
- openai SDK（httpx）默认读取 Windows 系统代理；如需直连 DashScope，在 llm.py 中传入 `httpx.AsyncClient(trust_env=False)`
- `done` 事件只由 `process_message()` 末尾统一发出，`finalize` 节点不再重复发送
- `confirming` 的 modify 分支在节点内完成字段更新，不回跳 `collect_extract`；description/image 变更走 `rag_and_confirm`，其他字段变更走 `re_confirm`（跳过 RAG）
- `confirming` 的 restart 分支清空 draft 后直接 finalize，"重新来"这句话不进入任何提取节点
- `pending_clarification` 保存上一轮 LLM 返回的澄清问题上下文，下一轮调用 `extract_fields` / `extract_fields_editing` 时透传，用完后清空
- `user_confirmed_description_priority` 在 COLLECTING / WAITING_IMAGE / CONFIRMING / PREVIEW_READY 四个阶段均可被 LLM 提取并写回 Session

## 持久化层

### 架构定位

本系统是**报修信息收集与确认层**，最终工单的存档、审核、修改都在下游业务系统。持久化层仅解决：

1. **会话状态跨进程/重启可恢复**——支持横向扩容、滚动发布、容器重启不丢草稿
2. **`repair_no` 跨进程不重复**——全局唯一工单号（用作幂等键/链路追踪）
3. **工单本身不持久化**——`session.ticket` 跟随 session TTL 即可，本系统不做工单档案库

### 存储后端切换

通过 `.env` 配置项 `storage_backend` 选择：

| 模式 | 适用场景 | 配置示例 |
|------|---------|---------|
| `memory` | 单进程开发/测试 | `storage_backend=memory` |
| `redis` | 生产/多 worker | `storage_backend=redis`<br>`redis_url=redis://localhost:6379/0` |

**切换方式**：修改 `.env` 后重启服务，`main.py` 的 `lifespan` 会自动初始化对应实现。

### 关键实现

- **SessionStore**（`services/session_store.py`）
  - `MemorySessionStore`：进程内 `dict + asyncio.Lock`
  - `RedisSessionStore`：`SETEX` 存 JSON 序列化的 session，`SET NX EX` 实现分布式锁
- **TicketCounter**（`services/ticket_counter.py`）
  - `MemoryTicketCounter`：`threading.Lock + counter`
  - `RedisTicketCounter`：`SETNX` 写种子 + `INCR` 原子递增
- **序列化**（`agent/state.py`）
  - `Session.serialize() / deserialize()`：完整字段映射（含 `pending_clarification`、`ticket` 等）
  - `TicketDraft.serialize() / deserialize()`：包含所有 RAG 填充字段
  - `_lock` 不序列化，每次从 Redis 读出后新建 `asyncio.Lock()`

### 并发控制

- **单进程内**：`Session._lock`（`asyncio.Lock`）串行同 session 的请求
- **多进程**：`store.acquire_lock(session_id, ttl_s=60)` 使用 Redis `SET NX` 实现分布式锁
- **锁已占用**：API 直接返回 `{"type": "error", "code": "BUSY"}`，不阻塞等待

### 一致性保证

- **节点过程中不写 Redis**：SSE 流式期间是只读快照，写回放在 `store.save(session)` 一次完成（锁释放前）
- **崩溃回滚代价**：最坏丢失本轮对话，用户可重新输入（可接受）
- **锁 TTL**：默认 60s，防进程崩溃死锁；正常完成后 `DEL` 显式释放

### 配置项

```bash
# 持久化后端
storage_backend=memory          # "memory" | "redis"
redis_url=redis://localhost:6379/0
redis_session_prefix=frap:session:
redis_lock_prefix=frap:lock:
redis_lock_ttl_seconds=60
repair_no_seed=1726198          # 工单号初始值
```
