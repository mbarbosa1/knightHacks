from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

'''
Reads DATABASE_URL, CORS_ORIGINS and the pool size from .env
'''
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env", extra="ignore"
    )
    database_url: SecretStr
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]
    db_pool_max_size: int = Field(default=5, ge=1, le=20)
    databricks_host: str | None = None
    databricks_http_path: str | None = None
    databricks_token: SecretStr | None = None
    databricks_genie_space_id: str | None = None
