# 工作流深度分析：facility-repair-agent

## 一、整体架构概览

```
用户浏览器（Lit Web Component）
        │  HTTP POST + SSE 流
        ▼
FastAPI 后端（/chat/init、/chat/message、/upload/image）
        │
        ▼
Agent 状态机（core.py）
    ├─ LLM 调用（services/llm.py → Qwen3 API）
    └─ RAG 检索（services/rag.py → ChromaDB + BAAI/bge）
```

---

## 二、完整工作流逐步拆解

### 步骤 1：会话初始化

**入口**：`POST /chat/init`（`api/v1/chat.py`）

```
前端调用 chatInit(clientId)
    → 后端 create_session()：生成 session_id、初始化 TicketDraft（全为 None）
    → state = GREETING
    → 将欢迎语写入 session.history（"您好！我是设施报修小助手……"）
    → 返回 { session_id, greeting, expires_in }
前端将 session_id 存入 sessionStorage
```

**关键对象**：
- `Session`：持有 state、history、draft、retry_count、过期时间
- `TicketDraft`：持有 description/estate/building/floor/unit/room/visit_time/image_urls 及 RAG 填充字段

---

### 步骤 2：用户发送消息

**入口**：`POST /chat/message`（`api/v1/chat.py`）

前端根据用户操作构造消息：
- 纯文字 → `{ type: "text", content: "..." }`
- 上传图片 → 先 `POST /upload/image` 拿到 `image_url`，再发 `{ type: "image_url", content: "图片已上传", image_url: "..." }`

后端收到请求后：
1. `get_session(session_id)` —— 查内存字典，过期返回 404
2. `refresh_session(session)` —— 重置过期时间
3. 启动 `EventSourceResponse(event_generator())` —— 以 SSE 流式推送事件

---

### 步骤 3：Agent 状态机路由（core.py）

`process_message()` 是核心入口，根据当前 `session.state` 做路由分发：

```
session.state == GREETING  → 自动切换为 COLLECTING，进入 _handle_collecting
session.state == COLLECTING → _handle_collecting
session.state == WAITING_IMAGE → _handle_waiting_image
session.state == CONFIRMING → _handle_confirming
session.state == COMPLETED/ESCALATED → 返回"已提交"提示，不再处理
```

这是一个**显式有限状态机**，每个消息只在当前状态对应的处理函数内执行，状态迁移由代码显式赋值（`session.state = AgentState.XXX`）。

---

### 步骤 4：COLLECTING 阶段——字段提取与意图识别

**函数**：`_handle_collecting(session, user_message, image_url)`

#### 4.1 LLM 字段提取（非流式）

调用 `llm.extract_fields(draft, user_message, image_url)`：

**Prompt 构造**：
```
System: EXTRACTION_SYSTEM（角色定义 + 提取规则 + 歧义处理规则 + JSON Schema）
User:   当前已知信息：{draft_json}
        用户本轮消息：{user_message}
        用户已上传图片：{image_url}（可选）
```

**提取结果 JSON Schema**：
```json
{
  "description": "空调不制冷",
  "estate": "前海嘉里中心",
  "building": "T25栋",
  "floor": "3楼",
  "unit": null,
  "room": null,
  "visit_time_text": "下午三点",
  "needs_human": false,
  "clarification_question": null
}
```

**Prompt 工程要点**：
- `response_format: json_object` —— 强制 Qwen3 输出纯 JSON，避免 markdown 包裹
- `temperature=0.1` —— 低温度保证提取稳定性
- `enable_thinking: false` —— 关闭思维链，减少 token 消耗
- 系统提示包含大量**示例对**（few-shot），覆盖各种楼层/楼栋格式歧义
- `draft_json` 作为上下文注入 —— LLM 知道哪些字段已有值，不会重复追问

#### 4.2 字段写入 Draft

`_apply_extraction(draft, extraction, image_url)` —— 只覆写非空字段，已有值不会被 null 覆盖。

#### 4.3 visit_time 解析（独立 LLM 调用）

若提取到 `visit_time_text`（如"下午三点"、"一小时后"）：
- 调用 `llm.resolve_visit_time(text, now)` —— 用 Qwen3 将自然语言转为绝对时间（"M月D日 H时mm分"）
- 代码层快速路径：模糊词（"随便"、"尽快"、"越快越好"等）直接返回 now+30min，不消耗 LLM

#### 4.4 意图路由（三分支）

```
extraction.needs_human == true
    → state = ESCALATED
    → yield { type: "human_service", partial_ticket }
    → 结束

extraction.clarification_question != null（字段歧义）
    → yield { type: "text_delta", content: 确认问题 }
    → 维持 COLLECTING 状态等待用户回答
    → 结束

以上均否 → 检查 draft.missing_required()
    ├─ 有缺失字段
    │   → retry_count++
    │   → 若重试超限且无 description → ESCALATED（转人工）
    │   → 否则流式生成追问 → yield text_delta chunks
    └─ 无缺失字段
        ├─ 无图片 → state = WAITING_IMAGE，询问图片
        └─ 有图片 → _run_rag_and_confirm()
```

---

### 步骤 5：追问回复生成（流式）

**函数**：`llm.generate_reply_stream(draft, history, missing)`

**Prompt 构造**：
```
System: 角色"小修" + 已收集字段 JSON + 缺失字段列表
        行动规则：合并追问、不要逐项列举、以已收集字段为准
Messages: [system] + session.history（完整对话历史）
```

**Prompt 工程要点**：
- `temperature=0.7` —— 相对高温度使追问更自然
- 注入完整 `session.history` —— LLM 知道完整对话上下文
- 明确规则"严格以已收集字段为准，不要根据对话历史推断" —— 防止 LLM 根据历史幻觉出字段值
- 缺失字段做人类可读转换（`missing_desc` 字典），避免把内部字段名暴露给 LLM

流式输出通过 `async for chunk in stream` 逐 token yield，前端实时追加显示。

---

### 步骤 6：WAITING_IMAGE 阶段

**函数**：`_handle_waiting_image(session, user_message, image_url)`

```
用户发来了 image_url → 写入 draft.image_urls → 进入 RAG+确认
用户包含跳过关键词（跳过/不用/算了/skip 等）→ 进入 RAG+确认
两者都没有 → 继续等待，提示上传或跳过
```

---

### 步骤 7：RAG 检索 + 确认摘要生成

**函数**：`_run_rag_and_confirm(session)`

#### 7.1 RAG 检索流程（rag.py）

```
draft.description（用户原始描述）
    │
    ▼  LLM 标准化（NORMALIZE_SYSTEM prompt，Qwen3）
    │  "A栋3楼302空调不制冷" → "空调不制冷"（剔除位置信息）
    ▼
BAAI/bge-large-zh-v1.5 Embedding（延迟加载 SentenceTransformer）
    │
    ▼
ChromaDB cosine 相似度检索（top-3）
    │
    ▼
取最高分 top-1：
    score < 0.30 → 返回 None（RAG 不介入，工单仍可生成）
    score 0.30-0.65 → low confidence
    score 0.65-0.85 → medium confidence
    score > 0.85 → high confidence
    │
    ▼
填充 draft：normalized_description / fault_type_code / fault_type_name
           / repair_priority_rag / repair_type
```

RAG 结果为 None 时，工单仍用默认值（fault_type="待分类"，priority="MEDIUM"）生成。

#### 7.2 visit_time 兜底

若 draft.visit_time 仍为空（全程未提及时间），自动赋值 now+30min。

#### 7.3 确认摘要生成（流式）

调用 `llm.generate_confirmation_stream(draft, history, visit_time)`：

```
System: 角色"小修" + 收集到的报修信息 JSON + 预计上门时间
        严格格式要求：• 位置 / • 问题 / • 上门时间 / 以上信息是否正确？
Messages: [system] + session.history
```

**Prompt 工程要点**：
- `temperature=0.3` —— 较低温度，格式输出稳定
- 明确禁止输出"图片：X张"、"已提交"等字段
- 最后一句"以上信息是否正确？确认后我将为您提交报修单。"要求原文输出
- state 切换为 `CONFIRMING`

---

### 步骤 8：CONFIRMING 阶段——用户确认判断

**函数**：`_handle_confirming(session, user_message)`

调用 `llm.check_user_confirmed(text)`，三层判断：

```
1. 关键词快速路径（无 LLM 调用）：
   命中否定词（不/错/改/取消）→ return False
   命中肯定词（好/是/对/确认/ok）且无前置否定词、非问句 → return True

2. 边界情况（问句/否定前缀/含义模糊）→ fallback 到 LLM：
   System: CONFIRM_CHECK_SYSTEM（严格判断规则）
   User:   用户消息
   max_tokens=5, temperature=0
   解析 "true"/"false"

3. LLM 调用失败 → return False（保守策略）
```

**结果路由**：
```
confirmed = True
    → build_ticket(session) 组装工单 JSON
    → state = COMPLETED
    → yield { type: "ticket_ready", ticket: {...} }
    → yield text_delta "好的，您的报修单已提交！"

confirmed = False（用户要修改）
    → state 回退到 COLLECTING
    → 再次 extract_fields 提取修改内容
    → 生成追问回复，告知已更新的字段
```

---

### 步骤 9：工单组装（ticket_builder.py）

```python
build_ticket(session) → {
    ticket_id: 随机 17 位数字
    repair_no: 全局递增计数器（1726198 起）
    order_status: "COMPLETED"
    repair_type: RAG 结果 or "公司报修"
    location: { estate, building, floor, unit }
    problem_description: normalized_description or description
    image_urls: [...]
    reporter: { name: null, phone: null }  # 当前版本不收集
    visit_time: "M月D日 H时mm分"
    repair_priority: RAG 结果 or "MEDIUM"
    fault_type: { code, displayName }
}
```

---

### 步骤 10：前端 SSE 事件处理

前端 `chat-store.ts` 的 `_handleSSE()` 根据事件类型路由：

| 事件类型 | 前端动作 |
|----------|----------|
| `text_delta` | 追加到 botMsg.content，实时渲染 |
| `state_update` | 更新 agentState、collectedFields |
| `ticket_ready` | 触发宿主 `onRepairTicketGenerated` CustomEvent，携带完整工单 JSON |
| `human_service` | 触发宿主 `onRequestHumanService` CustomEvent |
| `error` | 在气泡内显示错误信息 |
| `done` | 结束流，isStreaming = false |

---

## 三、LLM 调用汇总

| 调用位置 | 模式 | Prompt 类型 | temperature | 用途 |
|----------|------|-------------|-------------|------|
| `extract_fields` | 非流式 JSON | EXTRACTION_SYSTEM + few-shot | 0.1 | 字段提取 + 意图识别 |
| `generate_reply_stream` | 流式 | reply_system_prompt（动态） | 0.7 | 追问追问 |
| `generate_confirmation_stream` | 流式 | confirmation_system_prompt（动态） | 0.3 | 确认摘要 |
| `check_user_confirmed`（fallback） | 非流式 | CONFIRM_CHECK_SYSTEM | 0 | 确认判断 |
| `resolve_visit_time` | 非流式 | _RESOLVE_SYSTEM | 0 | 时间解析 |
| `_normalize_description`（RAG 前） | 非流式 | NORMALIZE_SYSTEM | 0.1 | 描述标准化 |

每次用户消息平均消耗 1-2 次 LLM 调用（提取必调，追问/确认/时间解析视情况）。

---

## 四、状态迁移完整图

```
                    ┌─────────────────────────────────┐
                    │          任何阶段均可触发          │
                    │   needs_human=true / 重试超限     │
                    └──────────────┬──────────────────┘
                                   ▼
GREETING ──(首条消息)──► COLLECTING ──(信息齐全+无图片)──► WAITING_IMAGE
                           │  ▲                                  │
                           │  │(用户修改确认)              (收到图片/跳过)
                           │  └────────────────────────────┐     │
                           │                               │     ▼
                           └──(信息齐全+有图片/跳过)───────► CONFIRMING
                                                                  │
                                                            (用户确认)
                                                                  │
                                                                  ▼
                                                            COMPLETED
                                                         (ticket_ready 事件)
                                              ▲
                                    ESCALATED (human_service 事件)
```

---

## 五、关键设计决策

### 5.1 为什么用显式状态机而非单一 LLM 决策？

- **可控性**：字段收集逻辑由代码决定，不依赖 LLM 理解"下一步该做什么"
- **成本**：每轮只调用必要的 LLM，不用每次让模型"规划"整个流程
- **可调试性**：`session.state` 是明确的断点，出问题能精确定位到哪个阶段

### 5.2 为什么 extract_fields 使用 few-shot + JSON Schema？

中文地址的多样性（T25栋、A1、3号楼、图书馆）需要大量示例才能稳定提取。`response_format: json_object` 消除了 markdown 包裹的解析开销，`_clean_json()` 作为最后保险。

### 5.3 为什么 RAG 检索在信息收集完成后而非每轮都触发？

RAG 需要完整的 `description`，信息收集阶段 description 可能不完整。检索一次即可，不需要随每轮消息更新。

### 5.4 check_user_confirmed 的三层设计

关键词快速路径覆盖 90% 场景，LLM fallback 处理"不对，是在嘉里"（含否定词+修改）、"好吗？"（问句）等边界情况，失败兜底 false（保守策略，宁可多问一次也不错误提交）。
