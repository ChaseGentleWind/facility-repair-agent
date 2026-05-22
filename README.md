# 设施报修智能客服 (Facility Repair Agent)

基于大模型的设施报修智能客服系统，通过对话式交互引导用户提交报修工单，结合 RAG 检索自动匹配故障类型和优先级。

## 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | Lit 3 (Web Components) + TypeScript + Vite (IIFE 单文件构建) |
| 后端 | FastAPI + Python 3.11 + uv 包管理 |
| 大模型 (运行时) | 通义千问 qwen3.5-omni-flash (DashScope OpenAI 兼容接口) |
| 大模型 (离线清洗) | DeepSeek-V4-flash |
| 向量模型 | BAAI/bge-large-zh-v1.5 (sentence-transformers) |
| 向量数据库 | ChromaDB (PersistentClient 文件存储) |
| 对象存储 | MinIO (S3 兼容，存储用户上传图片) |
| 部署 | Docker Compose (backend + frontend + minio) |

## 项目结构

```
facility-repair-agent/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 应用入口
│   │   ├── config.py            # 配置管理 (pydantic-settings)
│   │   ├── agent/
│   │   │   ├── core.py          # Agent 状态机主循环
│   │   │   ├── state.py         # 会话状态 & TicketDraft 数据模型
│   │   │   ├── prompts.py       # 所有 LLM Prompt 定义
│   │   │   └── ticket_builder.py # 工单构建器
│   │   ├── api/v1/
│   │   │   ├── chat.py          # /chat/init + /chat/message (SSE)
│   │   │   └── upload.py        # /upload/image (MinIO)
│   │   ├── services/
│   │   │   ├── llm.py           # LLM 调用封装 (提取/追问/确认)
│   │   │   ├── rag.py           # RAG 检索服务
│   │   │   └── storage.py       # MinIO 存储服务
│   │   └── models/
│   │       └── api_models.py    # Pydantic 请求/响应模型
│   ├── scripts/
│   │   ├── ingest_tickets.py    # 离线数据处理入口
│   │   ├── cleaner.py           # 历史工单清洗 (DeepSeek)
│   │   └── embedder.py          # 向量化 & ChromaDB 入库
│   ├── data/                    # 原始数据 & ChromaDB 持久化
│   ├── pyproject.toml           # Python 依赖定义
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── repair-agent.ts      # 根组件 <repair-agent>
│   │   ├── stores/chat-store.ts # 状态管理 (会话/消息/流式)
│   │   ├── components/          # UI 组件 (chat-panel, fab-button, input-bar, message-bubble, message-list)
│   │   ├── services/            # API 调用, SSE 解析, 图片压缩, 语音输入
│   │   ├── styles/theme.ts      # 设计令牌
│   │   └── types.ts             # TypeScript 类型定义
│   ├── index.html               # 演示页面
│   ├── nginx.conf               # 生产环境 Nginx 配置
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml           # 三服务编排
├── .env.example                 # 环境变量模板
└── README.md
```

## 系统流程

```
用户点击悬浮按钮 → 打开聊天面板 → 显示问候语
        │
        ▼
用户描述问题 (文字/语音/图片)
        │
        ▼
┌─────────────────────────────────────┐
│  COLLECTING 阶段                     │
│                                     │
│  1. LLM 字段提取 (extract_fields)    │
│     → description / building / floor │
│                                     │
│  2. 缺失字段? → 生成追问 (流式)       │
│     否 → 进入 RAG 检索               │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│  RAG 检索增强                        │
│                                     │
│  1. 描述标准化 (LLM → "实体+现象")    │
│  2. BGE 向量编码                     │
│  3. ChromaDB Top-3 检索              │
│  4. 匹配故障类型 + 优先级 + 置信度    │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│  CONFIRMING 阶段                     │
│                                     │
│  展示报修摘要 → 用户确认/修改         │
│  确认 → 生成工单                     │
│  修改 → 回到 COLLECTING              │
└─────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────┐
│  COMPLETED                           │
│                                     │
│  输出工单 (TKT-YYYYMMDD-XXXX)       │
│  触发 onRepairTicketGenerated 事件   │
└─────────────────────────────────────┘
```

**转人工路径：** 用户任何时候说"转人工"，或连续 3 轮未能收集到有效信息，自动触发 `onRequestHumanService` 事件。

## 工单输出示例

```json
{
  "ticket_id": "TKT-20260521-A3F7",
  "location": {
    "building": "T25栋",
    "floor": "3楼",
    "room": "302会议室"
  },
  "problem": {
    "description": "空调不制冷",
    "normalized_description": "空调不制冷",
    "fault_type_code": "03",
    "fault_type_name": "暖通类报修",
    "repair_priority": "HIGH",
    "repair_type": "维修",
    "confidence": "high",
    "rag_match_score": 0.9213
  },
  "image_urls": ["https://minio.example.com/facility-repairs/2026/05/21/abc123.jpg"],
  "metadata": {
    "session_id": "sess_a1b2c3d4e5f6",
    "source": "web",
    "created_at": "2026-05-21T10:30:00"
  }
}
```

## 快速开始

### 本地开发

```bash
# 后端
cd backend
cp .env.example .env  # 填入 QWEN_API_KEY
pip install uv
uv sync
uv run uvicorn app.main:app --host 0.0.0.0 --port 8500 --reload

# 前端
cd frontend
npm install
npm run dev
# 访问 http://localhost:5173
```

### Docker 部署

```bash
cp .env.example .env  # 填入 QWEN_API_KEY
docker compose up --build -d

# 前端: http://localhost
# MinIO 控制台: http://localhost:9001
```

### 离线数据处理 (RAG 知识库构建)

```bash
cd backend

# 完整流程：清洗 + 向量化
uv run python -m scripts.ingest_tickets

# 仅向量化（已有清洗结果时）
uv run python -m scripts.ingest_tickets --skip-clean
```

## 前端嵌入方式

构建产物为单个 IIFE 文件 `repair-agent.js`，可嵌入任意页面：

```html
<script src="https://your-cdn.com/repair-agent.js"
        data-config='{"apiBase":"/api","clientId":"your-client-id"}'>
</script>
```

监听事件获取工单结果：

```javascript
document.querySelector('repair-agent').addEventListener('onRepairTicketGenerated', (e) => {
  console.log('工单已生成:', e.detail)
})
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| QWEN_API_KEY | 通义千问 API Key | (必填) |
| QWEN_BASE_URL | DashScope 接口地址 | https://dashscope.aliyuncs.com/compatible-mode/v1 |
| QWEN_MODEL | 运行时模型 | qwen3.5-omni-flash |
| EMBEDDING_MODEL_PATH | 向量模型 | BAAI/bge-large-zh-v1.5 |
| CHROMA_PERSIST_DIR | ChromaDB 存储路径 | data/chromadb |
| MINIO_ENDPOINT | MinIO 地址 | minio:9000 |
| MINIO_ACCESS_KEY | MinIO 用户名 | minioadmin |
| MINIO_SECRET_KEY | MinIO 密码 | minioadmin |
| SESSION_TTL_SECONDS | 会话超时时间 | 1800 |
| MAX_RETRY_COUNT | 最大追问次数 | 3 |
| ALLOWED_ORIGINS | CORS 允许域名 | * |

## 架构特点

- **状态机驱动**：GREETING → COLLECTING → CONFIRMING → COMPLETED，流程清晰可控
- **SSE 流式响应**：追问和确认消息实时流式输出，用户体验流畅
- **RAG 置信度分级**：high (>0.85) / medium (>0.65) / low (>0.3)，下游系统可按置信度决策
- **Shadow DOM 隔离**：前端组件不受宿主页面 CSS 影响，安全嵌入任意系统
- **会话自动恢复**：后端会话过期时前端静默重连并重发消息，用户无感知
- **多模态输入**：支持文字、语音 (Web Speech API)、图片 (客户端压缩 + MinIO 存储)
