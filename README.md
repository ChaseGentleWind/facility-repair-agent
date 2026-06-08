# facility-repair-agent

企业设施报修 AI Agent：用户通过聊天描述故障，可上传现场图片；系统自动收集报修信息、辅助识别故障类型，并生成可提交的结构化工单预览。

## 功能概览

- **多轮信息收集**：自动提取故障描述、项目、楼栋、楼层、上门时间等必填字段。
- **图片辅助判断**：图片可选，用于补充现场信息和辅助故障识别。
- **RAG 故障分类**：基于历史工单向量检索，补全故障类型、优先级、维修类型。
- **工单确认与提交**：先展示确认卡片，再生成工单预览，用户确认后提交。
- **会话持久化**：支持内存模式和 Redis 模式，便于本地开发与生产部署。

## 技术栈

- 后端：Python 3.11、FastAPI、LangGraph、Qwen/DashScope、ChromaDB、Redis
- 前端：TypeScript、Lit Web Component、Vite、SSE 流式输出
- 存储：本地图片存储，Session 可选 Memory / Redis

## 目录结构

```text
backend/        FastAPI 后端、Agent 状态机、LLM/RAG/存储服务
frontend/       Lit Web Component 聊天挂件
docs/           工作流、架构和字段规则说明
CLAUDE.md       面向 Claude Code 的项目维护文档
```

## 快速启动

### 1. 后端

```powershell
cd backend
uv sync
Copy-Item .env.example .env
# 编辑 .env，填入 QWEN_API_KEY 等配置
uv run uvicorn app.main:app --reload --reload-dir app
```

后端默认接口前缀：`/api/v1`，Swagger 文档：`http://localhost:8000/docs`。

### 2. 前端

```powershell
cd frontend
npm install
npm run dev
```

生产构建：

```powershell
npm run build
```

## 主要接口

| 接口 | 说明 |
|------|------|
| `POST /api/v1/chat/init` | 初始化会话 |
| `POST /api/v1/chat/message` | 发送聊天消息，返回 SSE 流 |
| `POST /api/v1/upload/image` | 上传现场图片 |
| `POST /api/v1/ticket/submit` | 提交已生成的工单预览 |

## 常用验证

```powershell
cd backend
uv run pytest tests\agent tests\services\test_session_serialization.py tests\services\test_rag_relevance.py

cd ..\frontend
npm run build
```

## 更多文档

- [项目文档索引](docs/README.md)
- [Agent 工作流说明](docs/workflow-analysis.md)
- [LangGraph 架构现状](docs/langgraph-refactor-summary.md)
- [区域与房间字段规则](docs/plan-area-refactor.md)
