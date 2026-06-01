# LangGraph 重构完成总结

## 完成时间
2026-06-01

## 重构目标
将 `backend/app/agent/core.py` 中的手写异步状态机替换为 LangGraph 实现，保持 SSE 事件契约不变。

## 实施内容

### 1. 新增文件

- **app/agent/graph_state.py** - LangGraph 状态定义
  - `GraphState` TypedDict：session、user_message、image_url、events、路由标志
  
- **app/agent/edges.py** - 条件路由函数
  - 节点名常量（NODE_ENTRY、NODE_COLLECT_EXTRACT 等）
  - 路由函数：route_by_state、after_collect_extract、after_collect_decide 等
  
- **app/agent/nodes.py** - 状态机节点实现（357 行）
  - entry_router：写入历史，GREETING→COLLECTING 转换
  - collect_extract：LLM 字段提取，needs_human 检测
  - collect_decide：卡住检测（stall_count）
  - stream_reply：流式输出缺失字段追问
  - ask_image：请求图片上传
  - wait_image：处理图片或跳过
  - rag_and_confirm：RAG 检索 + 确认提示
  - confirming：三路意图分类（confirmed/restart/modify）
  - preview_edit：PREVIEW_READY 状态字段编辑
  - escalated/submitted/completed：终态节点
  - finalize：发送 done 事件
  
- **app/agent/draft_ops.py** - 纯函数工具
  - infer_location_from_area_or_room：从 area/room 推断 building/floor
  - clear_rag_fields：清空 RAG 填充字段
  - apply_extraction：应用 LLM 提取结果到 draft
  
- **app/agent/graph.py** - 图构建与兼容包装
  - build_graph()：构建 StateGraph，添加节点和条件边
  - compiled_graph：编译后的图实例（无 checkpointer）
  - process_message()：兼容包装函数，保持与旧 core.py 相同签名

### 2. 修改文件

- **app/api/v1/chat.py**
  - 导入：`agent.core` → `agent.graph`
  - 调用：`agent_core.process_message` → `agent_graph.process_message`

- **pyproject.toml**
  - 新增依赖：langgraph>=0.2.50、langchain-core>=0.3.0

### 3. 删除文件

- **app/agent/core.py** - 旧状态机实现（已完全替换）

### 4. 测试

- **tests/agent/test_graph_integration.py** - 集成测试（4 个用例全部通过）
  - test_graph_basic_flow：基本流程（mock LLM）
  - test_needs_human_flow：转人工流程
  - test_image_skip_flow：跳过图片流程
  - test_graph_structure：图结构完整性验证

## 技术决策

### 不使用 Checkpointer
原因：
1. Session 对象不可序列化（msgpack 报错）
2. 原代码已通过 `state.py` 的内存字典管理会话
3. LangGraph checkpointing 对此场景无实际价值

解决方案：
- 移除 MemorySaver
- 直接调用 `compiled_graph.astream(input_state, stream_mode="updates")`
- Session 状态由外部管理，图仅处理单次消息

### 状态传递
- `GraphState` 包含 `session` 引用，节点直接修改 session 对象
- 节点间通过 `_extraction`、`_proceed_to_rag`、`_intent`、`_need_rerag` 传递路由标志
- `events` 字段使用 `operator.add` reducer 累积 SSE 事件

### 图结构
```
entry_router (按 session.state 路由)
    ├─ COLLECTING → collect_extract
    │   ├─ _error → finalize
    │   ├─ needs_human → escalated
    │   ├─ clarification → finalize
    │   ├─ missing → collect_decide
    │   │   ├─ stalled → escalated
    │   │   └─ normal → stream_reply → finalize
    │   ├─ no_image → ask_image → finalize
    │   └─ complete → rag_confirm → finalize
    ├─ WAITING_IMAGE → wait_image
    │   ├─ proceed → rag_confirm → finalize
    │   └─ retry → finalize
    ├─ CONFIRMING → confirming
    │   ├─ confirmed → finalize
    │   ├─ restart/modify → collect_extract
    │   └─ unclear → finalize
    ├─ PREVIEW_READY → preview_edit
    │   ├─ need_rerag → rag_confirm → finalize
    │   └─ no_rerag → finalize
    ├─ ESCALATED → escalated → finalize
    ├─ SUBMITTED → submitted → finalize
    └─ COMPLETED → completed → finalize
```

## 验证结果

✅ 所有导入正常
✅ FastAPI 服务启动成功
✅ 集成测试 4/4 通过
✅ SSE 事件契约保持不变
✅ 业务逻辑完全保留

## 后续建议

1. **端到端测试**：启动前端，完整走通报修流程
2. **性能对比**：对比重构前后的响应时间和内存占用
3. **日志优化**：为关键节点添加结构化日志
4. **错误处理**：增强 LLM 调用失败时的降级策略
5. **文档更新**：更新 CLAUDE.md 中的架构说明

## 文件清单

新增：
- backend/app/agent/graph_state.py
- backend/app/agent/edges.py
- backend/app/agent/nodes.py
- backend/app/agent/draft_ops.py
- backend/app/agent/graph.py
- backend/tests/__init__.py
- backend/tests/agent/__init__.py
- backend/tests/agent/test_graph_integration.py

修改：
- backend/app/api/v1/chat.py
- backend/pyproject.toml

删除：
- backend/app/agent/core.py
