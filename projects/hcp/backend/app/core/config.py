"""Configuration for MAPI and S3 services."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings

# App runs from backend/ so .env lives one level up.
_ENV_FILE = "../.env"


class MapiSettings(BaseSettings):
    """HCP Management API configuration (from environment / .env).

    When ``hcp_domain`` is set and ``hcp_host`` is empty, ``hcp_host``
    is derived as ``admin.<domain>`` automatically.
    """

    model_config = {"env_file": _ENV_FILE, "extra": "ignore"}

    hcp_host: str = ""
    hcp_domain: str = ""
    hcp_port: int = 9090
    hcp_username: str = ""
    hcp_password: str = ""
    hcp_auth_type: Literal["hcp", "ad"] = "hcp"
    hcp_verify_ssl: bool = False
    hcp_timeout: int = 60

    def model_post_init(self, __context: Any) -> None:
        if not self.hcp_host and self.hcp_domain:
            self.hcp_host = f"admin.{self.hcp_domain}"


class S3Settings(BaseSettings):
    """HCP S3 data-plane configuration.

    Reuses HCP_USERNAME / HCP_PASSWORD for credential derivation.
    Only S3-specific values (endpoint, region) use the S3_ prefix.
    """

    model_config = {"env_file": _ENV_FILE, "extra": "ignore"}

    # Reuse same env vars as MAPI
    hcp_username: str = ""
    hcp_password: str = ""
    hcp_verify_ssl: bool = False

    # Shared with MAPI
    hcp_domain: str = ""

    # S3-specific (S3_ prefix in env)
    s3_endpoint_url: str = "https://s3.hcp.example.com"
    s3_region: str = "us-east-1"  # boto3 requires a region; HCP ignores it

    @property
    def endpoint_url(self) -> str:
        return self.s3_endpoint_url

    @property
    def region(self) -> str:
        return self.s3_region

    @property
    def verify_ssl(self) -> bool:
        return self.hcp_verify_ssl

    @property
    def access_key(self) -> str:
        """Base64-encoded username (HCP S3 convention)."""
        from app.core.auth_utils import derive_s3_keys

        return derive_s3_keys(self.hcp_username, self.hcp_password)[0]

    @property
    def secret_key(self) -> str:
        """MD5-hashed password (HCP S3 convention)."""
        from app.core.auth_utils import derive_s3_keys

        return derive_s3_keys(self.hcp_username, self.hcp_password)[1]


class StorageSettings(BaseSettings):
    """Backend-agnostic storage configuration.

    Controls which storage adapter is used (HCP, MinIO, or generic S3).
    HCP-specific settings are only relevant when ``storage_backend="hcp"``.
    """

    model_config = {"env_file": _ENV_FILE, "extra": "ignore"}

    storage_backend: Literal["hcp", "minio", "generic"] = "hcp"

    # S3-compatible settings (all backends)
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_verify_ssl: bool = False
    s3_addressing_style: Literal["auto", "path", "virtual"] = "auto"

    # Direct credentials (MinIO / generic)
    s3_access_key: str = ""
    s3_secret_key: SecretStr = SecretStr("")

    # HCP-specific (only when storage_backend=hcp)
    hcp_username: str = ""
    hcp_password: SecretStr = SecretStr("")
    hcp_domain: str = ""


class CacheSettings(BaseSettings):
    """Redis cache configuration."""

    model_config = {"env_file": _ENV_FILE, "extra": "ignore"}

    redis_url: str = ""  # Empty = no caching
    cache_default_ttl: int = 300  # 5 min — MAPI tenant/namespace listings
    cache_stats_ttl: int = 60  # 1 min — statistics, chargeback
    cache_config_ttl: int = 600  # 10 min — consoleSecurity, permissions, etc.
    cache_s3_list_ttl: int = 120  # 2 min — bucket/object listings
    cache_s3_meta_ttl: int = 300  # 5 min — head_bucket, head_object, ACLs
    cache_query_object_ttl: int = 60  # 1 min — metadata query object results
    cache_query_operation_ttl: int = 120  # 2 min — metadata query operation results
    cache_key_prefix: str = "hcp"


class AuthSettings(BaseSettings):
    """API authentication settings."""

    model_config = {"env_file": _ENV_FILE, "extra": "ignore"}

    api_secret_key: str = "change-me-in-production"
    api_token_expire_minutes: int = 480  # 8 hours
    cors_origins: str = ""  # Comma-separated origins, empty = allow all
