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
        # `populate_by_name` also teaches the env source the bare FIELD NAME as a second
        # lookup, so every alias below silently gained an un-namespaced twin. `env_prefix`
        # redirects that fallback onto the namespace the aliases already declare; an explicit
        # alias bypasses it, so the deliberately-bare ones still land. See
        # tests/unit/test_settings_env_namespace.py.
        populate_by_name=True,
        env_prefix="RASK_",
    )

    api_prefix: str = Field(default="/api/v1", alias="RASK_API_PREFIX")
    cors_origins: list[str] = Field(default_factory=list, alias="RASK_CORS_ORIGINS")

    #: Whether this app serves `/docs`, `/redoc` and `/openapi.json`. OFF by default, and the default
    #: is the whole point.
    #:
    #: Four services already carried a `docs_enabled` flag before this one existed — and it defaulted
    #: to True, with no deployment path setting it either way (`grep -rn DOCS chart/ .docker/ scripts/`
    #: matched nothing). A flag nobody sets IS the default, so all four shipped their schemas openly
    #: while looking configurable. A security default that every deployment must remember to turn off
    #: is one nobody turns off.
    #:
    #: What this closes is a route table, parameter names and request/response schemas — not data, and
    #: every documented route stays individually auth-gated. It matters most on the gateway, whose
    #: aggregated `{prefix}/openapi.json` is published at the Ingress and answers an anonymous caller
    #: by fanning out to every backend it fronts.
    docs_enabled: bool = Field(default=False, alias="RASK_DOCS")

    #: Whether this app emits the `lance.audit` compliance stream. ON by default, because the trail
    #: it gates is evidence: a deployment that wants none says so, and one that forgets still has it.
    #:
    #: THE FLAG EXISTS SO THE TRAIL IS DECIDED RATHER THAN INHERITED. `governed/audit.py` gates the
    #: stream by the dedicated logger's LEVEL, and a service that never sets it leaves `lance.audit`
    #: at NOTSET — inheriting the root level `setup_logging` takes from `RASK_LOG_LEVEL`. Measured
    #: 2026-09-09 across the ten services holding `audit()` call sites: three set it and seven
    #: inherited, so for those seven `RASK_LOG_LEVEL=WARNING` — the documented volume lever — deleted
    #: the compliance trail, and this flag could not turn it off. `make_service_app` now applies it,
    #: for the same reason it applies the readiness flags: a convention most apps did not hold up is
    #: not a convention. Pinned by
    #: `test_the_audit_trail_is_armed_by_configuration_not_by_the_log_level.py`.
    audit_enabled: bool = Field(default=True, alias="RASK_AUDIT_ENABLED")

    ray_dashboard_url: str = Field(default="http://localhost:8265", alias="RAY_DASHBOARD_URL")

    http_timeout: float = Field(default=15.0, alias="RASK_HTTP_TIMEOUT")

    #: The request-body ceiling every app built by `make_service_app` enforces (`body_limit.py`).
    #:
    #: A DoS BOUND, not a business rule. It exists so a multi-GB body cannot reach a buffer — five
    #: apps had no ceiling at all until 2026-08-26, the gateway included, and the gateway buffers
    #: every proxied body whole. Generous on purpose: a route that wants a real limit states its own,
    #: and the catalog sets 256 MiB explicitly for Arrow-IPC writes (it builds its own app, so it is
    #: not double-capped).
    max_body_bytes: int = Field(default=64 * 1024 * 1024, alias="RASK_MAX_BODY_BYTES")

    # OpenTelemetry opt-in. When true, OTLP/HTTP traces are exported to the
    # endpoint configured by OTEL_EXPORTER_OTLP_ENDPOINT. Also auto-enabled when
    # that env var is present, even if this flag is false.
    otel_enabled: bool = Field(default=False, alias="RASK_OTEL_ENABLED")

    # Dapr service invocation. When false, build_dapr_client returns None and the
    # gateway falls back to direct httpx upstreams. DAPR_HTTP_PORT is set by the
    # Dapr sidecar injector in-cluster.
    dapr_enabled: bool = Field(default=False, alias="RASK_DAPR_ENABLED")
    dapr_http_port: str = Field(default="3500", alias="DAPR_HTTP_PORT")
