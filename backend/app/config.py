from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    qwen_api_key: str
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen3.5-omni-flash"

    session_ttl_seconds: int = 1800
    max_retry_count: int = 3

    allowed_origins: str = "*"

    @property
    def origins_list(self) -> list[str]:
        if self.allowed_origins == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins.split(",")]


settings = Settings()
