"""flows-only settings.

`service_kit.config.Settings` stays the shared base (api_prefix, CORS, Dapr, OTel) and is read the
way every fleet service reads it — `make_service_app` builds it and the lifespan puts it on
`app.state.settings`. This class holds the fields that are nobody else's business, in the same
pydantic-settings shape, so a flows-only knob does not widen the estate-wide config class.

The Serve ORIGIN is the important one. It is `:8000` — Ray Serve's own HTTP ingress — which is a
different port and a different protocol from the Ray dashboard (`:8265`) that `settings.ray_dashboard_url`
names and that `/api/serve/*` proxies read-only. Introspection and invocation are two doors; this is
the invocation one.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed.settings import GovernedAuthSettings


class FlowsSettings(GovernedAuthSettings, BaseSettings):
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
        env_prefix="LANCE_",
    )

    #: Ray Serve's HTTP ingress origin. A `model` node POSTs to `{serve_url}/{app}`.
    serve_url: str = Field(default="http://localhost:8000", alias="RASK_FLOWS_SERVE_URL")

    #: Per-node Serve call budget. Generous because an HTR page through `/htrflow` is a real
    #: pipeline, not a token stream — the models zone's own inference route allows 180 s.
    serve_timeout: float = Field(default=180.0, alias="RASK_FLOWS_SERVE_TIMEOUT")

    #: How many finished runs stay readable at `GET /flows/runs/{id}`. The store is a process-local
    #: dict (v0), so this is a memory bound, not a retention policy — oldest is evicted first.
    max_runs: int = Field(default=200, alias="RASK_FLOWS_MAX_RUNS")


def build_flows_settings() -> FlowsSettings:
    """Read once, at startup. `model_config.env_file` makes pydantic-settings read `.env` itself —
    the same mechanism `service_kit.build_settings` relies on — so there is no second dotenv load."""
    return FlowsSettings.model_validate({})
