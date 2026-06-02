# 工作流分析：facility-repair-agent

## 一、架构概览

```
用户浏览器（Lit Web Component）
        │  HTTP POST + SSE 流
        ▼
FastAPI 后端（/chat/init、/chat/message、/upload/image、/ticket/submit）
        │
        ▼
LangGraph Agent（graph.py → compiled_graph）
    ├─ 13 个节点（nodes.py）
    ├─ 条件路由（edges.py）
    ├─ LLM 调用（services/llm.py → Qwen3 via DashScope）
    └─ RAG 检索（services/rag.py → ChromaDB + BAAI/bge-large-zh-v1.5）
```

---

## 二、LangGraph 图结构

### 节点清单

| 节点 | 职责 |
|------|------|
| `entry_router` | 写入 history，GREETING→COLLECTING 转换，按 session.state 分派 |
| `collect_extract` | LLM 字段提取，检测 needs_human / clarification / missing |
| `collect_decide` | stall_count 检测，超限转 ESCALATED |
| `stream_reply` | 流式生成缺失字段追问 |
| `ask_image` | 发送请求图片上传提示 |
| `wait_image` | 处理图片输入或跳过指令 |
| `rag_and_confirm` | RAG 检索 + 流式生成确认摘要 |
| `confirming` | 三路意图分类（confirmed / restart / modify） |
| `preview_edit` | PREVIEW_READY 状态下字段编辑 |
| `escalated` | 发送转人工事件，设置 ESCALATED 状态 |
| `submitted` | 发送已提交提示 |
| `completed` | 发送已完成提示 |
| `finalize` | 发送 `done` 事件，结束图执行 |

### 图拓扑

```
entry_router
    ├─ COLLECTING → collect_extract
    │   ├─ _error ──────────────────────────────► finalize
    │   ├─ needs_human ──► escalated ───────────► finalize
    │   ├─ clarification ───────────────────────► finalize
    │   ├─ missing → collect_decide
    │   │   ├─ stalled ──► escalated ───────────► finalize
    │   │   └─ normal ──► stream_reply ─────────► finalize
    │   ├─ no_image ──► ask_image ──────────────► finalize
    │   └─ complete ──► rag_and_confirm ─────────► finalize
    ├─ WAITING_IMAGE → wait_image
    │   ├─ proceed ──► rag_and_confirm ──────────► finalize
    │   └─ retry ────────────────────────────────► finalize
    ├─ CONFIRMING → confirming
    │   ├─ confirmed ────────────────────────────► finalize
    │   └─ restart/modify ──► collect_extract（循环）
    ├─ PREVIEW_READY → preview_edit
    │   ├─ need_rerag ──► rag_and_confirm ───────► finalize
    │   └─ no_rerag ─────────────────────────────► finalize
    ├─ ESCALATED → escalated ────────────────────► finalize
    ├─ SUBMITTED → submitted ────────────────────► finalize
    └─ COMPLETED → completed ────────────────────► finalize
```

### GraphState 字段

```python
class GraphState(TypedDict):
    session: Session          # 会话对象，节点直接修改
    user_message: str         # 本轮用户输入
    image_url: str | None     # 本轮图片 URL
    events: Annotated[list[dict], operator.add]  # SSE 事件累积
    _extraction: dict         # collect_extract 提取结果
    _proceed_to_rag: bool     # wait_image 后是否进入 RAG
    _intent: str              # confirming 后意图
    _need_rerag: bool         # preview_edit 后是否重新 RAG
```

---

## 三、Session 状态机

```
GREETING → COLLECTING → WAITING_IMAGE → CONFIRMING → PREVIEW_READY → SUBMITTED
                ↓（任何阶段）
            ESCALATED（转人工）
            COMPLETED（外部回调，预留）
```

**关键对象**：
- `Session`：state、history、draft、stall_count、image_description、user_confirmed_description_priority
- `TicketDraft`：必填字段（description/estate/building/floor/visit_time）+ RAG 字段（fault_type_code/name、repair_priority_rag、repair_type）

---

## 四、完整工作流

### 1. 会话初始化
`POST /chat/init` → 生成 session_id → state = GREETING → 返回欢迎语

### 2. COLLECTING 阶段（collect_extract → collect_decide / stream_reply）

**字段提取**（单次 VLM 调用）：
```python
extraction = await llm.extract_fields(draft, user_message, image_url)
# 返回：image_description_text（有图片时）+ 结构化字段
# 失败时返回 {"_error": "llm_call_failed"}
```

**关键特性**：
- 有图片时先生成 2-3 句自然语言描述，再提取字段
- 自动推断楼层（302→3楼、1205→12楼、7S1→7楼）
- 图文矛盾时设置 `clarification_question` 询问用户
- 识别"以我的为准"等表述，设置 `user_confirmed_description_priority=true`

**时间解析**：
- 模糊词（"随便"、"尽快"）→ now+30min
- 自然语言（"下午三点"、"一小时后"）→ LLM 解析为 "M月D日 H时mm分"

**意图路由**（edges.after_collect_extract）：
```
_error → finalize（提示"系统繁忙"）
needs_human → escalated
clarification_question → finalize（输出问题）
missing_required() → collect_decide
  stall_count 超限 → escalated
  正常 → stream_reply → finalize
无缺失 + 无图片 → ask_image → finalize
无缺失 + 有图片 → rag_and_confirm → finalize
```

### 3. WAITING_IMAGE 阶段（wait_image）
- 收到图片 → 提取字段 → `_proceed_to_rag=True` → rag_and_confirm
- 用户跳过（关键词：跳过/不用/没有/算了/skip）→ `_proceed_to_rag=True` → rag_and_confirm
- 其他 → finalize（继续等待）

### 4. RAG 检索 + 确认摘要（rag_and_confirm）

**RAG 流程**：
```
用户描述 + 图片（可选）
    ↓
图文语义冲突检测（LLM 判断）
    ├─ 冲突（不同故障类型）→ 只用描述
    └─ 互补（同一故障不同方面）→ 拼接增强
    ↓
标准化描述（剔除位置信息）
    ↓
BAAI/bge-large-zh-v1.5 Embedding
    ↓
ChromaDB 检索（top-3，cosine 相似度）
    ↓
score < 0.30 → 返回 None
score ≥ 0.30 → 填充 fault_type_code/name、repair_priority_rag、repair_type
```

**visit_time 兜底**：若仍为空 → now+30min

**确认摘要**（流式生成，纯基于 draft，不依赖 history）：
```
好的，我来帮您确认一下报修信息：
  • 位置：前海嘉里中心 T25栋 3楼
  • 问题：空调不制冷
  • 上门时间：5月26日 15时00分

以上信息是否正确？确认后我将为您提交报修单。
```

**状态切换**：COLLECTING / WAITING_IMAGE → CONFIRMING

### 5. CONFIRMING 阶段（confirming）

**确认判断**（三层逻辑）：
1. 关键词快速路径：否定词（不/错/改）→ False，肯定词（好/是/确认）→ True
2. LLM fallback（边界情况）：max_tokens=10, temperature=0
3. 失败兜底：False

**结果路由**（edges.after_confirming）：
- `_intent=confirmed` → finalize（build_ticket → state=PREVIEW_READY → yield `ticket_ready`）
- `_intent=modify` → collect_extract（用 `extract_fields_editing` 提取修改字段）
- `_intent=restart` → collect_extract（清空 draft，重新收集）
- `_intent=unclear` → finalize（追问用户）

### 6. PREVIEW_READY 阶段（preview_edit）

工单预览已生成，等待用户操作：
- 用户修改字段 → `extract_fields_editing` 提取 → `_need_rerag=True` → rag_and_confirm → CONFIRMING
- 前端调用 `POST /ticket/submit` → state=SUBMITTED

---

## 五、LLM 调用汇总

| 调用位置 | 模式 | temperature | 用途 |
|----------|------|-------------|------|
| `extract_fields` | 非流式 JSON | 0.1 | 字段提取 + 图片描述 |
| `extract_fields_editing` | 非流式 JSON | 0.1 | 修改阶段字段提取 |
| `generate_reply_stream` | 流式 | 0.4 | 追问缺失字段（最近 10 条 history） |
| `generate_confirmation_stream` | 流式 | 0.3 | 确认摘要（纯 draft，无 history） |
| `check_user_confirmed` | 非流式 | 0 | 确认判断 fallback |
| `classify_denial_intent` | 非流式 | 0 | 否认意图分类 |
| `resolve_visit_time` | 非流式 | 0 | 时间解析 |
| `_check_semantic_conflict` | 非流式 | 0 | 图文语义冲突检测 |
| `_describe_image_fault` | 非流式 VLM | 0.1 | 图片故障描述（RAG 增强） |

**错误处理**：所有 LLM 调用失败返回 `{"_error": "llm_call_failed"}`，节点检测后提示"系统繁忙"并维持当前状态。

---

## 六、关键设计决策

### 6.1 LangGraph 替代手写状态机
图结构使节点职责单一、路由逻辑集中在 edges.py，便于独立测试和扩展。不使用 Checkpointer——Session 状态由外部内存字典管理，图仅处理单次消息。

### 6.2 图文语义冲突自动检测
RAG 检索前调用 LLM 判断图文是否冲突：
- **冲突**（不同故障类型）→ 只用用户描述
- **互补**（同一故障不同方面）→ 拼接增强
- 用户明确"以我的为准" → 跳过图片（`ignore_image=true`）

### 6.3 单次 VLM 调用合并图片描述 + 字段提取
有图片时一次调用同时生成描述和提取字段，延迟减半、一致性保证。

### 6.4 LLM 失败降级
所有 LLM 调用失败时返回带 `_error` 标记的 sentinel dict，节点检测后向用户提示"系统繁忙"并保持当前状态不变。

### 6.5 History 窗口限制
`generate_reply_stream` 只传最近 10 条 history，避免 token 浪费。`generate_confirmation_stream` 完全不依赖 history，纯基于 draft 生成摘要。

### 6.6 系统代理绕过
openai SDK（httpx）默认读取 Windows 注册表系统代理。若代理不可用会导致 502 连接失败。在 `services/llm.py` 中传入 `httpx.AsyncClient(trust_env=False)` 可强制直连 DashScope。

---

## 七、前端 SSE 事件

| 事件类型 | 前端动作 |
|----------|----------|
| `text_delta` | 追加到气泡，实时渲染 |
| `state_update` | 更新 agentState、collectedFields |
| `ticket_ready` | 触发 `onRepairTicketGenerated` CustomEvent |
| `human_service` | 触发 `onRequestHumanService` CustomEvent |
| `error` | 显示错误信息 |
| `done` | 结束流 |

---

## 八、状态迁移图

```
                    ┌─────────────────────────────┐
                    │   任何阶段均可触发            │
                    │   needs_human / 重试超限     │
                    └──────────┬──────────────────┘
                               ▼
                           ESCALATED

GREETING ──► COLLECTING ──► WAITING_IMAGE
               │  ▲              │
               │  │              │
               │  └──────────────┘
               │
               └──► CONFIRMING ──► PREVIEW_READY ──► SUBMITTED
                      │  ▲              │
                      │  └──────────────┘
```

---

## 九、启动说明

```bash
# 后端（避免 data/uploads/ 触发热重载）
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8500 --reload --reload-dir app

# 前端
cd frontend
npm run dev
```

等待日志出现 `🎉 RAG 模型预热完成，服务已就绪` 后再发送请求。
