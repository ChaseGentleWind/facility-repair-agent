# LangGraph 重构方案

## Context

当前 `backend/app/agent/core.py` 是一个 580 行的手写异步状态机：每条用户消息进入 `process_message`，按 `session.state` 分派到 `_handle_collecting / _handle_waiting_image / _handle_confirming / _handle_preview_ready` 四个长函数，再通过 `async for ... yield` 把 SSE 事件吐给 API 层。问题在于：

- 状态分支与副作用（LLM 调用、RAG 检索、stall 检测、字段清理、人工托管）糅在一起，新增一个分支要同时改 5 处。
- LLM/RAG 调用没有统一抽象，重试、缓存、追踪都要在每个 handler 单独加。
- Session 持久化只有内存 dict，没有断点恢复，也无法做时间旅行调试。
- 测试只能在 `process_message` 顶层做端到端，单个状态难以隔离。

目标是用 LangGraph 把状态机重写为有向图：每个状态成为一个节点，转移成为带条件函数的边，副作用收敛到节点函数内部，对外 SSE 契约**完全不变**——前端 `agent_state_machine.ts` 已知的事件类型 (`text_delta` / `state_update` / `ticket_ready` / `human_service` / `error` / `done`) 与字段一字不改。Checkpointer 选 `MemorySaver`，持久化语义等价于现状（重启即丢失），先把图跑起来，Sqlite/Redis 可后续再换。

## 总体策略

- **完全替换** `agent/core.py`：删除旧文件，新建 `agent/graph.py` + `agent/nodes.py` + `agent/edges.py`。`api/v1/chat.py` 的 `agent_core.process_message(...)` 调用入口签名保持不变（仍是 `AsyncIterator[dict]`），由新的 `graph.py` 提供。
- **不动的部分**：`services/llm.py`、`services/rag.py`、`services/storage.py`、`agent/prompts.py`、`agent/ticket_builder.py`、`agent/state.py`（仅扩展，见下）、`api/v1/*`、`models/api_models.py`、整个 `frontend/`。
- **三类节点抽象**：
  1. **路由节点**：纯函数，依据 `session.state` 与消息内容选择下一节点（替代当前 `process_message` 顶层 if 链）。
  2. **业务节点**：包裹一次状态推进所需的副作用（LLM 提取、RAG、字段填充、stall 检测、ticket 构建）。
  3. **工具调用**：LLM/RAG 仍走 `services/llm.py` 函数式 API，**不**改造成 LangChain Tool——避免引入不必要的抽象层。

## 依赖变更

`backend/pyproject.toml` 新增：

```
langgraph>=0.2.50
langchain-core>=0.3.0
```

不安装 `langchain-openai`：项目用的是 DashScope OpenAI 兼容接口，已经有 `services/llm.py` 直接用 `openai` SDK 调；强行换成 LangChain `ChatOpenAI` 会丢掉现有的 `enable_thinking: false` 控制和图片 data-uri 编码逻辑，得不偿失。

## 图结构设计

### State Schema（`agent/graph_state.py`，新建）

```python
class GraphState(TypedDict):
    session: Session                      # 复用现有 Session dataclass，承载 draft / history / stall_count 等
    user_message: str
    image_url: str | None
    events: Annotated[list[dict], operator.add]  # SSE 事件累积通道，节点 append、reducer 合并
```

所有节点接收 `GraphState`，返回 `{"events": [...], ...}` 增量。`Session` 仍保存在 `state._store`（`agent/state.py`），LangGraph 的 `MemorySaver` 用于图本身的 checkpoint，与 Session 内存存储互不干扰。

### 节点清单（`agent/nodes.py`，新建）

| 节点 | 对应原逻辑 | 主要副作用 |
|------|-----------|-----------|
| `entry_router` | `process_message` 顶层 state 分派 + `GREETING → COLLECTING` 自动迁移 | 仅写 `session.history` |
| `collect_extract` | `_handle_collecting` 前半段（`extract_fields` + 位置推断 + visit_time 解析 + needs_human 兜底） | 调 LLM、写 draft |
| `collect_decide` | `_handle_collecting` 后半段（缺失字段判断 + stall 检测 + ESCALATED） | 写 stall_count |
| `ask_image` | 进入 `WAITING_IMAGE` 的提示语 | 写 history、emit text_delta |
| `wait_image` | `_handle_waiting_image` | 同 collect_extract |
| `rag_and_confirm` | `_run_rag_and_confirm` | 调 RAG + `generate_confirmation_stream` |
| `confirming` | `_handle_confirming`（`check_user_confirmed` + `classify_denial_intent` 三分支） | 调 LLM、build_ticket |
| `preview_edit` | `_handle_preview_ready`（含图文一致性二次 VLM、area/room 重推） | 调 LLM、视情况清空 RAG 字段 |
| `escalated` / `submitted` / `completed` | 终态文案 | 仅 emit text_delta |
| `stream_reply` | 包裹 `generate_reply_stream` 流式输出 + history 写入 | 调 LLM |

`stream_reply` 单独抽出来是因为它要把 LLM token 流转成多条 `text_delta` 事件——后面"流式输出适配"会展开。

### 边（`agent/edges.py`，新建）

LangGraph 用 `add_conditional_edges(source, condition_fn, mapping)` 表达分支。关键条件函数：

- `route_by_state(state) -> str`：读 `session.state`，返回节点名。承接 `entry_router` 出口。
- `after_collect_extract(state) -> str`：根据 `extraction["_error"]` / `needs_human` / `clarification_question` / `missing` / 是否已有 `image_urls`，分别去 `END` / `escalated` / `END` / `stream_reply` / `ask_image` / `rag_and_confirm`。
- `after_confirming(state) -> str`：`confirmed` → `END`（已 emit ticket_ready）；`intent=restart` → `END`；`intent=modify` → `collect_extract`（继续在同一轮内处理修改）。

每条边映射到的"下一节点"在文件顶端常量集中管理，避免散落字符串拼写错误。

### 入口/出口

- 入口：`entry_router`
- 出口：所有节点最终汇到一个 `finalize` 节点，负责 emit `{"type":"done"}`，等价于现 core.py 第 54 行的统一 done。

## 流式输出适配（关键点）

LangGraph 节点内的"流式 LLM 输出"和"图本身的事件流"是两层东西，需要打通：

- 节点函数声明为 `async def`，并用 LangGraph 的 `astream(..., stream_mode="custom")` + `get_stream_writer()` 在节点内边算边把 SSE 事件 push 出来。
- `api/v1/chat.py` 的 `event_generator` 改为：

```python
async for chunk in graph.astream(
    {"session": session, "user_message": user_text, "image_url": image_url, "events": []},
    config={"configurable": {"thread_id": session.session_id}},
    stream_mode="custom",
):
    yield {"data": json.dumps(chunk, ensure_ascii=False)}
```

这样 `generate_reply_stream` / `generate_confirmation_stream` 内部的 token 流可以原样转成 `text_delta`，而节点级别的 `state_update` / `ticket_ready` / `human_service` 走同一个 writer——前端拿到的事件序列与现在逐字节一致。

`session._lock` 保留：仍由 `chat.py` 在调 `graph.astream` 前后包裹，LangGraph 的 checkpointer 不替代会话级互斥。

## 关键约定保留清单

以下行为在重构后必须逐一回归，CLAUDE.md 已列出，重构 PR 必须有对应测试或人工验证：

- `needs_human` 触发词必须包含"人工"或"客服"——挪到 `collect_extract` 节点末尾，逻辑不变。
- RAG 返回 `None` 时工单仍可生成——`rag_and_confirm` 节点继续容错。
- 跳过图片关键词集合 `{跳过/不用/没有/算了/不需要/skip}`——挪到 `wait_image` 节点。
- LLM 全部 `enable_thinking: false`——不动 `services/llm.py`，约定自然保留。
- LLM 失败返回 `{"_error": "llm_call_failed"}`，调用方提示"系统繁忙"——节点函数检测后 emit text_delta + state_update 后 return（不进 `stream_reply`）。
- `generate_reply_stream` 只传最近 10 条 history——已在 `services/llm.py` 内部，无需变动。
- `generate_confirmation_stream` 不依赖 history——同上。
- area/room 互斥与位置推断 (`_infer_location_from_area_or_room`)、`_apply_extraction`、`_clear_rag_fields`——抽到 `agent/draft_ops.py` 作为纯函数复用，不再藏在 core.py 末尾。

## 文件落地清单

新建：

- `backend/app/agent/graph.py` — `build_graph()`、模块级 `compiled_graph`（带 MemorySaver）、`process_message(session, user_text, image_url)` 兼容包装函数（保持 `api/v1/chat.py` 调用方无感）。
- `backend/app/agent/graph_state.py` — `GraphState` TypedDict。
- `backend/app/agent/nodes.py` — 全部节点函数。
- `backend/app/agent/edges.py` — 条件路由函数 + 节点名常量。
- `backend/app/agent/draft_ops.py` — 从 core.py 末尾迁出的纯函数 (`_apply_extraction` / `_infer_location_from_area_or_room` / `_clear_rag_fields`)。
- `backend/tests/agent/test_graph_smoke.py` — 关键路径回归。

修改：

- `backend/app/agent/state.py` — 不动数据结构；仅在文件末尾导出 `MemorySaver` 单例（避免每次 import 重建）。
- `backend/app/api/v1/chat.py` — 把 `agent_core.process_message(...)` 改为调用 `graph.process_message(...)`，函数签名不变；事件透传逻辑不变。
- `backend/pyproject.toml` — 新增 `langgraph` / `langchain-core`。

删除：

- `backend/app/agent/core.py`（逻辑全部迁移完毕后）。

## 验证

1. **依赖安装**：`cd backend && uv sync`（`pyproject.toml` 改完后）。
2. **单元回归**（`backend/tests/agent/test_graph_smoke.py`）：
   - 用户首条消息 → `entry_router` 走 `GREETING→COLLECTING` 分支并触发 `collect_extract`。
   - 缺失字段连续 3 轮不变 → `ESCALATED`，emit `human_service`。
   - 全字段齐 + 无图 → `ask_image` 提示。
   - 全字段齐 + 跳过图 → `rag_and_confirm` → `CONFIRMING` 摘要。
   - CONFIRMING 阶段用户回 "好的" → emit `ticket_ready`，state = PREVIEW_READY。
   - PREVIEW_READY 改描述未换图 → 触发 VLM 二次比对分支。
3. **本地端到端**：
   - `cd backend && uv run python run.py` 起后端。
   - `cd frontend && npm run dev` 起前端。
   - 浏览器跑一遍：文字描述 → 上传图 → 确认 → 修改楼层 → 提交。对照旧版本，前端 chat 气泡序列、`onRepairTicketGenerated` 回调时机、`agentState` 转移应一致。
4. **SSE 字节级对比**（可选）：用同一组录制好的用户输入分别打到旧/新分支，`curl -N` 抓 SSE 流 diff，确认 `data:` 行只有时间戳级别的差异。
5. **回滚**：本次完全替换，回滚靠 `git revert` 单个重构 commit。

## 不在本次范围

- 把 LLM 调用换成 LangChain `ChatOpenAI` / `bind_tools`。
- Session 存储换成 Redis。
- 工单提交后的外部回调（`COMPLETED` 状态外部触发链路）。
- Checkpointer 升级到 SQLite/Redis——MemorySaver 即可，等持久化真正成为需求再换。
