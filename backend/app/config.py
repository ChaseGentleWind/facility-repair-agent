from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    qwen_api_key: str
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.5-omni-flash"

    embedding_model_path: str = "BAAI/bge-large-zh-v1.5"
    chroma_persist_dir: str = str(_BASE_DIR / "data" / "chromadb")

    use_local_storage: bool = True
    local_upload_dir: str = str(_BASE_DIR / "data" / "uploads")

    session_ttl_seconds: int = 1800
    max_stall_count: int = 3

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"

    allowed_origins: str = "*"

    # 持久化配置
    storage_backend: str = "memory"  # "memory" | "redis"
    redis_url: str = "redis://localhost:6379/0"
    redis_session_prefix: str = "frap:session:"
    redis_lock_prefix: str = "frap:lock:"
    redis_lock_ttl_seconds: int = 60
    repair_no_seed: int = 1726198

    # LLM 调用治理
    llm_timeout_seconds: float = 15.0
    llm_max_retries: int = 3

    @property
    def origins_list(self) -> list[str]:
        if self.allowed_origins == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins.split(",")]


settings = Settings()
