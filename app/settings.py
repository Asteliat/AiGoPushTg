from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)
    bot_token: str = ""
    api_secret_key: str = ""
    admin_ids: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    app_env: str = "development"
    log_level: str = "INFO"
    # Legacy default credentials: an existing PostgreSQL volume already has them.
    database_url: str = "postgresql+asyncpg://atcivperson:change-me-before-deploy@localhost:5432/atcivperson"
    redis_url: str = "redis://localhost:6379/0"
    data_dir: Path = Path("./data")
    ai_enabled: bool = False
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    ai_timeout_seconds: float = 45.0
    proxy_encryption_key: str = ""
    log_chat_id: int = 0

    @property
    def admin_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_ids.split(",") if x.strip().isdigit()}

@lru_cache
def get_settings() -> Settings:
    return Settings()
