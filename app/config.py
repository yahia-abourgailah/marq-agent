"""
Application settings.

Values come from the environment file selected by APP_ENV.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# [claude] APP_ENV now actually selects the environment file. It was
# hardcoded to ".env.development", so the README's environment table was
# fiction: staging and production silently loaded development settings, or
# failed at import when that file was absent.
#
# Read from os.environ rather than Settings itself — the value has to be
# known before the model is constructed, because it decides which file
# the model reads.
APP_ENV = os.getenv("APP_ENV", "development")
ENV_FILE = f".env.{APP_ENV}"


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

    # [claude] Workspace — user-uploaded files.
    #
    # All three are optional with defaults, so existing environment files
    # keep working unchanged.
    #
    # `workspace_root` holds uploaded files and their parsed content. It is
    # deliberately outside the repository tree by default: uploads contain
    # customer data, and a directory under the working tree is one `git add
    # -A` away from being committed.
    workspace_root: str = "./var/workspace"

    # Separate from `qdrant_collection` on purpose. That one is reserved for
    # CRM-derived vectors; mixing user uploads into it would make the
    # per-workspace payload filter the only thing separating a user's files
    # from application data.
    workspace_collection: str = "marq_workspace"

    # [claude] Embedded-Qdrant directory, used only when qdrant_url is empty.
    # Lets local development run without a Qdrant server; see build_index().
    # Not a production configuration — set QDRANT_URL instead.
    qdrant_path: str = "./var/qdrant"

    # Multilingual on purpose — see app/workspace/embeddings.py.
    embedding_model: str = (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )

    model_name: str
    model_base_url: str
    model_api_key: str

    # [claude] Defaults to 0.0 — greedy decoding.
    #
    # The SQL Agent is effectively a compiler: one question should map to one
    # query. Sampling there produces intermittently different SQL and makes
    # any eval a coin flip rather than a measurement.
    #
    # Optional with a default, so existing environment files keep working.
    model_temperature: float = 0.0

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,  # [claude] was hardcoded to ".env.development"
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    # [claude] Settings is instantiated at import time (see below), so a
    # misconfigured environment surfaces as a ten-error pydantic traceback
    # from whichever module happened to import app.config first. When the
    # cause is simply a missing env file, say so plainly instead.
    #
    # Real environment variables are still honoured when no file exists —
    # the friendly error only replaces the message when validation has
    # already failed AND the expected file is absent.
    try:
        return Settings()
    except ValidationError as exc:
        if not Path(ENV_FILE).is_file():
            raise RuntimeError(
                f"Missing environment file '{ENV_FILE}' "
                f"(APP_ENV={APP_ENV!r}).\n"
                f"Copy the template and fill it in:\n"
                f"    cp {ENV_FILE}.example {ENV_FILE}\n"
                f"Or set the variables in the environment directly."
            ) from exc
        raise


settings = get_settings()


__all__ = [
    "APP_ENV",  # [claude]
    "ENV_FILE",  # [claude]
    "Settings",
    "get_settings",
    "settings",
]
