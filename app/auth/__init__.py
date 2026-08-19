"""[claude] Authentication for the HTTP API."""

from app.auth.jwt import AuthError, TokenVerifier
from app.auth.principal import Principal, workspace_id_for

__all__ = [
    "AuthError",
    "Principal",
    "TokenVerifier",
    "workspace_id_for",
]
