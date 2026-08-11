from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Marquise application database
    postgres_host: str
    postgres_port: int = 5432
    postgres_user: str
    postgres_password: str
    postgres_db: str

    # CRM PostgreSQL database
    # Optional for now — we will add the credentials when we connect
    # to the CRM database.
    crm_postgres_host: str | None = None
    crm_postgres_port: int = 5432
    crm_postgres_user: str | None = None
    crm_postgres_password: str | None = None
    crm_postgres_db: str | None = None

    # Redis
    redis_url: str

    # Qdrant
    qdrant_url: str
    qdrant_collection: str

    model_name: str
    model_base_url: str
    model_api_key: str

    model_config = SettingsConfigDict(
        env_file=".env.development",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


__all__ = ["Settings", "get_settings", "settings"]