# 工作流分析：facility-repair-agent

## 一、架构概览

```
用户浏览器（Lit Web Component）
        │  HTTP POST + SSE 流
        ▼
FastAPI 后端（/chat/init、/chat/message、/upload/image、/ticket/submit）
        │
        ▼
Agent 状态机（core.py）
    ├─ LLM 调用（services/llm.py → Qwen3.5-omni-flash）
    └─ RAG 检索（services/rag.py → ChromaDB + BAAI/bge-large-zh-v1.5）
```

---

## 二、核心状态机

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

## 三、完整工作流

### 1. 会话初始化
`POST /chat/init` → 生成 session_id → state = GREETING → 返回欢迎语

### 2. COLLECTING 阶段

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

**意图路由**：
```
needs_human=true → ESCALATED
_error → 提示"系统繁忙"，维持当前状态
clarification_question → 输出问题，维持 COLLECTING
missing_required() → 流式生成追问（最近 10 条 history）
无缺失+无图片 → WAITING_IMAGE
无缺失+有图片 → RAG 检索 + 确认摘要
```

### 3. WAITING_IMAGE 阶段
- 收到图片 → 提取字段 → RAG 检索
- 用户跳过（关键词：跳过/不用/没有/算了/skip）→ RAG 检索
- 其他 → 继续等待

### 4. RAG 检索 + 确认摘要

**RAG 流程**（`_run_rag_and_confirm()`）：
```
用户描述 + 图片（可选）
    ↓
图文语义冲突检测（LLM 判断）
    ├─ 冲突（如"灯坏了" vs "墙面水渍"）→ 只用描述
    └─ 互补（如"处理一下" + "挂钟放桌上"）→ 拼接增强
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

**状态切换**：COLLECTING → CONFIRMING

### 5. CONFIRMING 阶段

**确认判断**（三层逻辑）：
1. 关键词快速路径：否定词（不/错/改）→ False，肯定词（好/是/确认）→ True
2. LLM fallback（边界情况）：max_tokens=10, temperature=0
3. 失败兜底：False

**结果路由**：
- confirmed=True → build_ticket() → state=PREVIEW_READY → yield `ticket_ready`
- confirmed=False → classify_denial_intent() 判断意图：
  - modify：回到 COLLECTING，用 `extract_fields_editing` 提取修改字段
  - restart：清空 draft，回到 COLLECTING
  - unclear：追问用户想修改什么

### 6. PREVIEW_READY 阶段

工单预览已生成，等待用户操作：
- 用户修改字段 → `extract_fields_editing` 提取 → 重新 RAG + 确认 → 回到 CONFIRMING
- 前端调用 `POST /ticket/submit` → state=SUBMITTED

---

## 四、LLM 调用汇总

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

**错误处理**：所有 LLM 调用失败返回 `{"_error": "llm_call_failed"}`，调用方检测后提示用户"系统繁忙"并维持当前状态。

---

## 五、关键设计决策

### 5.1 图文语义冲突自动检测

RAG 检索前调用 LLM 判断图文是否冲突：
- **冲突**（不同故障类型）→ 只用用户描述
- **互补**（同一故障不同方面）→ 拼接增强
- 用户明确"以我的为准" → 跳过图片（`ignore_image=true`）

### 5.2 单次 VLM 调用合并图片描述 + 字段提取

有图片时一次调用同时生成描述和提取字段，延迟减半、一致性保证。

### 5.3 显式状态机 vs 单一 LLM 决策

流程由代码决定，不依赖 LLM 理解"下一步该做什么"。每轮只调用必要的 LLM，`session.state` 是明确的断点。

### 5.4 LLM 失败降级

所有 LLM 调用失败时返回带 `_error` 标记的 sentinel dict，状态机检测后向用户提示"系统繁忙"并保持当前状态不变，避免静默丢失信息。

### 5.5 History 窗口限制

`generate_reply_stream` 只传最近 10 条 history，避免 token 浪费和幻觉。`generate_confirmation_stream` 完全不依赖 history，纯基于 draft 生成摘要。

---

## 六、前端 SSE 事件

| 事件类型 | 前端动作 |
|----------|----------|
| `text_delta` | 追加到气泡，实时渲染 |
| `state_update` | 更新 agentState、collectedFields |
| `ticket_ready` | 触发 `onRepairTicketGenerated` CustomEvent |
| `human_service` | 触发 `onRequestHumanService` CustomEvent |
| `error` | 显示错误信息 |
| `done` | 结束流 |

---

## 七、状态迁移图

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
