# facility-repair-agent

企业设施报修 AI Agent——用户通过聊天（文字/图片）描述故障，系统自动收集信息并生成结构化工单 JSON。

## 技术栈

- **后端**：Python FastAPI + Qwen3（DashScope OpenAI 兼容接口）
- **RAG**：ChromaDB + BAAI/bge-large-zh-v1.5
- **前端**：TypeScript Lit Web Component，SSE 流式输出
- **图片**：本地存储（默认）或 MinIO

## 后端结构 (`backend/app/`)

| 路径 | 作用 |
|------|------|
| `agent/state.py` | AgentState 枚举、TicketDraft、Session |
| `agent/core.py` | Agent 状态机主循环 |
| `agent/prompts.py` | 所有 Prompt 模板 |
| `agent/ticket_builder.py` | 组装工单 JSON |
| `services/llm.py` | Qwen API 封装 |
| `services/rag.py` | ChromaDB 检索 |
| `api/v1/chat.py` | /chat/init、/chat/message |
| `api/v1/upload.py` | /upload/image |
| `api/v1/ticket.py` | /ticket/submit |

## Agent 状态机

```
GREETING → COLLECTING → WAITING_IMAGE → CONFIRMING → PREVIEW_READY → SUBMITTED
                                ↓（任何阶段）
                           ESCALATED（转人工）
```

- CONFIRMING：用户确认后生成工单 → PREVIEW_READY
- PREVIEW_READY：等待用户提交或修改字段（修改后重新 RAG + 确认）
- SUBMITTED：前端调用 POST /ticket/submit 后进入
- COMPLETED：外部系统回调更新（预留）

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
