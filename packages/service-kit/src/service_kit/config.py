"""Settings loaded from environment + .env via pydantic-settings.

Read once at startup via `Settings()`; pass through DI as `SettingsDep`. Never
re-read env vars in routes or services.

Only the fields the surviving fleet (gateway + ray) reads live here — the
viewer-I/O, Lance search and SPA fields died with core-api/search-api/volumes-api
in the R6/R20 media wave (docs/architecture/lance-ns-merge.md). S3 endpoint and
credential resolution belongs to `storage` (env-driven: RASK_S3_* / AWS_* /
HCP_* aliases), not to this class.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    api_prefix: str = Field(default="/api/v1", alias="RASK_API_PREFIX")
    cors_origins: list[str] = Field(default_factory=list, alias="RASK_CORS_ORIGINS")

    ray_dashboard_url: str = Field(default="http://localhost:8265", alias="RAY_DASHBOARD_URL")

    http_timeout: float = 15.0

    # OpenTelemetry opt-in. When true, OTLP/HTTP traces are exported to the
    # endpoint configured by OTEL_EXPORTER_OTLP_ENDPOINT. Also auto-enabled when
    # that env var is present, even if this flag is false.
    otel_enabled: bool = Field(default=False, alias="RASK_OTEL_ENABLED")

    # Dapr service invocation. When false, build_dapr_client returns None and the
    # gateway falls back to direct httpx upstreams. DAPR_HTTP_PORT is set by the
    # Dapr sidecar injector in-cluster.
    dapr_enabled: bool = Field(default=False, alias="RASK_DAPR_ENABLED")
    dapr_http_port: str = Field(default="3500", alias="DAPR_HTTP_PORT")
