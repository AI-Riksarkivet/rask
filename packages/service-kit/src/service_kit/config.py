"""Settings loaded from environment + .env via pydantic-settings.

Read once at startup via `Settings()`; pass through DI as `SettingsDep`. Never
re-read env vars in routes or services.
"""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    viewer_input: str = Field(alias="RASK_VIEWER_INPUT")
    viewer_output: str = Field(alias="RASK_VIEWER_OUTPUT")

    api_prefix: str = Field(default="/api/v1", alias="RASK_API_PREFIX")
    cors_origins: list[str] = Field(default_factory=list, alias="RASK_CORS_ORIGINS")

    # S3 endpoint URL for any S3-compatible backend (MinIO / rustfs / AWS).
    # rask is storage-agnostic: this is just an endpoint. The canonical name is
    # RASK_S3_ENDPOINT_URL; S3_ENDPOINT_URL (the boto3-ecosystem name) is also
    # accepted, plus HCP_ENDPOINT as the Helm chart's legacy bridge (drop once the
    # chart sets RASK_S3_ENDPOINT_URL).
    s3_endpoint_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("RASK_S3_ENDPOINT_URL", "S3_ENDPOINT_URL", "HCP_ENDPOINT"),
    )
    # Skip TLS verification (self-signed endpoints, e.g. local MinIO/rustfs).
    s3_insecure: bool = Field(
        default=False,
        validation_alias=AliasChoices("RASK_S3_INSECURE", "S3_INSECURE", "HCP_INSECURE"),
    )
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")

    search_bucket: str = Field(default="images-batch-search", alias="RASK_SEARCH_BUCKET")
    cache_bucket: str = Field(default="images-batch", alias="RASK_CACHE_BUCKET")
    output_bucket: str = Field(default="images-batch-alto", alias="RASK_OUTPUT_BUCKET")
    iiif_url: str = Field(default="https://iiifintern-ai.ra.se", alias="RASK_IIIF_URL")
    source_mode: Literal["iiif", "s3"] = Field(default="iiif", alias="RASK_SOURCE_MODE")
    lines_table: str = Field(default="lines", alias="RASK_LINES_TABLE")
    catalog_table: str = Field(default="archive_catalog", alias="RASK_CATALOG_TABLE")
    ray_dashboard_url: str = Field(default="http://localhost:8265", alias="RAY_DASHBOARD_URL")

    repo_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[4])
    spa_build_dir: Path | None = Field(default=None, alias="RASK_SPA_BUILD")

    http_timeout: float = 15.0
    lance_query_timeout_seconds: int = Field(default=30, ge=1, alias="RASK_LANCE_QUERY_TIMEOUT_SECONDS")

    # OpenTelemetry opt-in. When true, OTLP/HTTP traces are exported to the
    # endpoint configured by OTEL_EXPORTER_OTLP_ENDPOINT. Also auto-enabled when
    # that env var is present, even if this flag is false.
    otel_enabled: bool = Field(default=False, alias="RASK_OTEL_ENABLED")

    # Dapr service invocation. When false, build_dapr_client returns None and the
    # gateway falls back to direct httpx upstreams. DAPR_HTTP_PORT is set by the
    # Dapr sidecar injector in-cluster.
    dapr_enabled: bool = Field(default=False, alias="RASK_DAPR_ENABLED")
    dapr_http_port: str = Field(default="3500", alias="DAPR_HTTP_PORT")

    @property
    def resolved_spa_build(self) -> Path:
        return self.spa_build_dir or (self.repo_root / "components" / "apps" / "frontend" / "build")

    @property
    def lance_db_uri(self) -> str:
        return f"s3://{self.search_bucket}"

    def lance_storage_options(self) -> dict[str, str] | None:
        if not self.s3_endpoint_url or not self.aws_access_key_id or not self.aws_secret_access_key:
            return None
        opts = {
            "aws_endpoint": self.s3_endpoint_url,
            "aws_access_key_id": self.aws_access_key_id,
            "aws_secret_access_key": self.aws_secret_access_key,
            "aws_region": self.aws_region,
            "aws_virtual_hosted_style_request": "false",
            "allow_http": "true" if self.s3_endpoint_url.startswith("http://") else "false",
        }
        # A self-signed endpoint (local MinIO/rustfs) is skipped by boto3 via
        # the insecure flag, but LanceDB's Rust S3 client (object_store) verifies
        # by default and fails the TLS handshake ("error sending request"). Mirror
        # the insecure flag so the search tables can be opened over https.
        if self.s3_insecure:
            opts["allow_invalid_certificates"] = "true"
        return opts
