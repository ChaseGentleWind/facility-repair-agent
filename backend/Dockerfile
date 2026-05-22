FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir uv -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app/ app/
COPY run.py .

RUN mkdir -p data/chromadb

ENV HF_ENDPOINT=https://hf-mirror.com

EXPOSE 8500

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8500"]
