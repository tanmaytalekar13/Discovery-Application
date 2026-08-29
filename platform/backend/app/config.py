from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    arcadedb_host: str = Field(min_length=1)
    arcadedb_port: int = Field(default=2480, ge=1, le=65535)
    arcadedb_database: str = Field(min_length=1)
    arcadedb_user: str = Field(min_length=1)
    arcadedb_password: str = Field(min_length=1)
    reliability_threshold: float = Field(default=0.75, ge=0, le=1)
    discovery_mode: str = Field(default="mock", pattern="^(mock|live|mixed)$")
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"


@lru_cache
def get_settings() -> Settings:
    return Settings()