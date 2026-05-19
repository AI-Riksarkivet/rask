"""HCPSettings — configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class HCPSettings(BaseSettings):
    """Configuration for the HCP client.

    All values can be set via ``HCP_``-prefixed environment variables,
    e.g. ``HCP_ENDPOINT``, ``HCP_USERNAME``, ``HCP_TENANT``.
    """

    model_config = SettingsConfigDict(env_prefix="HCP_")

    endpoint: str = "http://localhost:8000/api/v1"
    username: str = ""
    password: str = ""
    tenant: str | None = None
    timeout: float = 30.0
    max_retries: int = 4
    retry_base_delay: float = 1.0
    multipart_threshold: int = 100 * 1024 * 1024  # 100 MB
    multipart_chunk: int = 64 * 1024 * 1024  # 64 MB
    multipart_concurrency: int = 6
    verify_ssl: bool = True
