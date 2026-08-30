"""The gateway's settings, and why the front door needed a class of its own.

It was the ONE service in the fleet with no settings module at all: sixteen raw `os.environ.get()`
calls scattered across `_routes()`, `_target_base()`, the sidecar-route blocklist, the lifespan and
module import — three of them re-evaluated on every proxied request. `service_kit.config.Settings`
already declares `api_prefix` (RASK_API_PREFIX), `dapr_enabled` (RASK_DAPR_ENABLED) and
`dapr_http_port` (DAPR_HTTP_PORT) under exactly these aliases, so those reads were re-implementations
of fields the estate owns, with no validation and no single read point.

Two things that cost, and neither is style:

* **`.env` reached only some of the reads.** `_routes()` called `load_dotenv()`; the `RASK_DOCS` read
  did not, and it runs at module IMPORT — before anything has called `_routes()`. One config file,
  two answers, decided by which line happened to run first. pydantic-settings reads `.env` itself, in
  one source with one precedence, so that ordering question no longer exists (and `load_dotenv` is
  gone with it).
* **Configuration was re-decided per request.** `_target_base()` read `RASK_DAPR_ENABLED` and
  `DAPR_HTTP_PORT` on every proxied call, so the process environment changing under a running server
  silently re-routed live traffic between the sidecar and the direct upstream. Startup config that
  can move mid-flight is config nothing can reason about.

**Subclasses `BaseSettings` directly, NOT `service_kit.config.Settings`.** The gateway builds its own
`FastAPI` rather than going through `make_service_app`, and it owns no CORS surface, no OTel
endpoints of its own and no Lance/S3 knobs — inheriting the whole shared class would declare a
config surface the gateway does not have and cannot honour. The three shared aliases are re-declared
here VERBATIM (`RASK_API_PREFIX`, `RASK_DAPR_ENABLED`, `DAPR_HTTP_PORT`) so a value set for the fleet
means the same thing at the edge.

**Upstream URLs are one field per row.** They were positional pairs inside `_routes()`, which is why
a new backend meant a new `os.environ.get` rather than a new field; here the whole upstream surface
is enumerable, which is what `docs/getting-started/configuration.md` documents.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """Every knob the gateway reads, in one validated model built once per process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # NO `populate_by_name` and NO `env_prefix`: every field below carries an explicit alias that
        # is already the deployed environment variable's full name, so there is nothing for a bare
        # field-name fallback to bind that would not be a second, undocumented spelling.
    )

    #: Where the fleet mounts its API. The chart and `scripts/dev-micro.sh` both set `/api`; the code
    #: default stays `/api/v1` so an un-configured process is not silently the deployed shape.
    api_prefix: str = Field(default="/api/v1", alias="RASK_API_PREFIX")

    #: The merged `/docs` + `/openapi.json` pair, OFF by default. `chart/templates/ingress.yaml`
    #: publishes `/api`, so these two routes are internet-reachable wherever they exist — and they
    #: are the only routes the gateway answers itself under the prefix, taking no dependency and
    #: checking no token.
    docs_enabled: bool = Field(default=False, alias="RASK_DOCS")

    #: Route through this pod's own Dapr sidecar instead of dialling the upstream directly.
    dapr_enabled: bool = Field(default=False, alias="RASK_DAPR_ENABLED")

    #: Dapr's own HTTP port, injected by daprd. Only read when `dapr_enabled`.
    dapr_http_port: str = Field(default="3500", alias="DAPR_HTTP_PORT")

    #: Comma-separated lineage route-name prefixes that must never be reachable from the public edge
    #: (`chart/templates/_helpers.tpl`'s `lance.lineageSidecarOnlyRoutes` renders this).
    lineage_sidecar_only_routes: str = Field(
        default="lineage-events,lineage-reconcile-cron",
        alias="RASK_LINEAGE_SIDECAR_ONLY_ROUTES",
    )

    # --- one field per upstream, in the order the route table lists them ---------------------
    compute_url: str = Field(default="http://127.0.0.1:8804", alias="RASK_COMPUTE_URL")
    controlplane_url: str = Field(default="http://127.0.0.1:8820", alias="RASK_CONTROLPLANE_URL")
    catalog_url: str = Field(default="http://127.0.0.1:2333", alias="RASK_CATALOG_API_URL")
    lineage_url: str = Field(default="http://127.0.0.1:8000", alias="RASK_LINEAGE_API_URL")
    medallion_url: str = Field(default="http://127.0.0.1:8002", alias="RASK_MEDALLION_API_URL")
    explorer_viewer_url: str = Field(default="http://127.0.0.1:8101", alias="RASK_EXPLORER_VIEWER_URL")
    explorer_search_url: str = Field(default="http://127.0.0.1:8102", alias="RASK_EXPLORER_SEARCH_URL")
    explorer_annotator_url: str = Field(default="http://127.0.0.1:8103", alias="RASK_EXPLORER_ANNOTATOR_URL")
    ingest_url: str = Field(default="http://127.0.0.1:8830", alias="RASK_INGEST_URL")
    flows_url: str = Field(default="http://127.0.0.1:8840", alias="RASK_FLOWS_URL")
    notifications_url: str = Field(default="http://127.0.0.1:8850", alias="RASK_NOTIFICATIONS_URL")

    @field_validator("api_prefix")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """`/api/` and `/api` must build the same route table — every row concatenates onto this."""
        return value.rstrip("/")

    @property
    def sidecar_only_routes(self) -> tuple[str, ...]:
        """The blocklist as the case-folded prefixes the middleware matches on."""
        return tuple(route.strip().lower() for route in self.lineage_sidecar_only_routes.split(",") if route.strip())


def build_gateway_settings() -> GatewaySettings:
    """Read the environment once.

    Deliberately NOT `lru_cache`d: the gateway module is reloaded per test with a different
    environment, and a process-wide cache would hand the second reload the first one's answer. The
    process builds this exactly twice — once at import for the two routes FastAPI needs at
    construction time (`docs_url`/`openapi_url`), once in the lifespan for `app.state.settings`,
    which is what every request reads.
    """
    return GatewaySettings.model_validate({})
