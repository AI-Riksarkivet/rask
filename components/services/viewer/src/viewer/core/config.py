"""Settings loaded from environment + .env via pydantic-settings.

Read once at startup via `Settings()`; pass through DI as `SettingsDep`. Never
re-read env vars in routes or services.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    viewer_input: str = Field(alias="RASK_VIEWER_INPUT")
    viewer_output: str = Field(alias="RASK_VIEWER_OUTPUT")

    api_prefix: str = Field(default="/api/v1", alias="RASK_API_PREFIX")
    cors_origins: list[str] = Field(default_factory=list, alias="RASK_CORS_ORIGINS")

    hcp_endpoint: str | None = Field(default=None, alias="HCP_ENDPOINT")
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")

    search_bucket: str = Field(default="images-batch-search", alias="RASK_SEARCH_BUCKET")
    lines_table: str = Field(default="lines", alias="RASK_LINES_TABLE")
    catalog_table: str = Field(default="archive_catalog", alias="RASK_CATALOG_TABLE")
    ray_dashboard_url: str = Field(default="http://localhost:8265", alias="RAY_DASHBOARD_URL")

    repo_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[6])
    batches_db: Path | None = Field(default=None, alias="RASK_BATCHES_DB")
    spa_build_dir: Path | None = Field(default=None, alias="RASK_SPA_BUILD")

    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    db_pool_size: int = Field(default=10, ge=1, le=100, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, ge=0, le=200, alias="DB_MAX_OVERFLOW")
    db_pool_recycle_seconds: int = Field(default=1800, ge=60, alias="DB_POOL_RECYCLE_SECONDS")
    db_pool_timeout_seconds: int = Field(default=30, ge=1, alias="DB_POOL_TIMEOUT_SECONDS")
    db_query_timeout_seconds: int = Field(default=60, ge=1, alias="DB_QUERY_TIMEOUT_SECONDS")

    http_timeout: float = 15.0
    ray_proxy_timeout: float = 15.0
    lance_query_timeout_seconds: int = Field(default=30, ge=1, alias="RASK_LANCE_QUERY_TIMEOUT_SECONDS")

    @property
    def resolved_batches_db(self) -> Path:
        return self.batches_db or (self.repo_root / ".cache" / "batches.db")

    @property
    def resolved_database_url(self) -> str:
        """Effective DB URL — explicit `DATABASE_URL` wins, else sqlite at `resolved_batches_db`.

        Same code drives both sqlite (dev) and postgres (prod):
            sqlite+aiosqlite:///<path>
            postgresql+asyncpg://user:pass@host/dbname
        """
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.resolved_batches_db}"

    @property
    def resolved_spa_build(self) -> Path:
        return self.spa_build_dir or (self.repo_root / "components" / "apps" / "frontend" / "build")

    @property
    def lance_db_uri(self) -> str:
        return f"s3://{self.search_bucket}"

    def lance_storage_options(self) -> dict[str, str] | None:
        if not self.hcp_endpoint or not self.aws_access_key_id or not self.aws_secret_access_key:
            return None
        return {
            "aws_endpoint": self.hcp_endpoint,
            "aws_access_key_id": self.aws_access_key_id,
            "aws_secret_access_key": self.aws_secret_access_key,
            "aws_region": self.aws_region,
            "aws_virtual_hosted_style_request": "false",
            "allow_http": "true" if self.hcp_endpoint.startswith("http://") else "false",
        }
