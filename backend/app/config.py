from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    qwen_api_key: str
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.5-omni-flash"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"

    embedding_model_path: str = "BAAI/bge-large-zh-v1.5"
    chroma_persist_dir: str = str(_BASE_DIR / "data" / "chromadb")

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "facility-repairs"
    minio_secure: bool = False
    minio_public_base: str = ""

    session_ttl_seconds: int = 1800
    max_retry_count: int = 3

    allowed_origins: str = "*"

    @property
    def origins_list(self) -> list[str]:
        if self.allowed_origins == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins.split(",")]


settings = Settings()
