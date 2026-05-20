# 设施报修 Agent 后端

## 快速启动

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # 填入真实的 QWEN_API_KEY
python run.py
```

服务启动后访问 http://localhost:8000/docs 查看 Swagger 文档。

## 测试对话流程

```bash
# 1. 初始化会话
curl -X POST http://localhost:8000/api/v1/chat/init \
  -H "Content-Type: application/json" \
  -d '{"client_id":"test_001"}'

# 2. 发送消息（替换 session_id）
curl -N -X POST http://localhost:8000/api/v1/chat/message \
  -H "Content-Type: application/json" \
  -d '{"session_id":"YOUR_SESSION_ID","message":{"type":"text","content":"A栋3楼302会议室空调不制冷"}}'
```

## 环境变量

| 变量 | 说明 |
|------|------|
| `QWEN_API_KEY` | DashScope API Key |
| `QWEN_BASE_URL` | 模型接口地址 |
| `QWEN_MODEL` | 模型名称 (qwen3.5-omni-flash) |
| `SESSION_TTL_SECONDS` | 会话过期时间 |
| `ALLOWED_ORIGINS` | CORS 允许的域名 |
