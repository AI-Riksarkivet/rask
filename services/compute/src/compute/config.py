"""The compute service's settings, and why it needs its own class.

It rode `service_kit`'s generic `Settings`, which carries the shared knobs and NONE of the estate's
auth knobs — so the problem was not "auth is off here" but "auth CANNOT be turned on here". With no
`GovernedAuthSettings` there is no `LANCE_OIDC_ENABLED` to bind, and no chart value could gate this
service however the estate was configured.

What that left open: `compute` proxies the Ray dashboard using a token the chart deliberately turns
ON (`chart/templates/rayservice.yaml`, `ray.auth.enabled` → `RAY_AUTH_TOKEN`), and the gateway carries
`{prefix}/ray` and `/api/serve` to the public edge. Jobs, actors, tasks, cluster state, driver logs
and the whole Serve status API, to anonymous callers. `proxy.py`'s own comment — "Never widen this
without an auth layer in front of /api" — was an acknowledgement that no such layer existed.

Subclasses the shared `Settings` rather than `BaseSettings` directly: this service reads
`ray_dashboard_url` and the rest of the common surface, and a second parallel settings object would
be two answers to "what is the dashboard URL".
"""

from pydantic import Field

from service_kit.config import Settings
from service_kit.governed.settings import GovernedAuthSettings


class ComputeSettings(GovernedAuthSettings, Settings):
    """The shared settings plus the estate's auth knobs. Every added field defaults OFF."""

    ray_client_retry_cooldown_s: float = Field(
        default=10.0,
        ge=0.0,
        # Explicit, service-scoped alias: without one, `Settings`' env_prefix bound the bare
        # RASK_RAY_CLIENT_RETRY_COOLDOWN_S while every record of the knob names the COMPUTE_ form —
        # so the documented name was a no-op and the knob was untunable in any deployment.
        alias="RASK_COMPUTE_RAY_CLIENT_RETRY_COOLDOWN_S",
        description="Minimum seconds between JobSubmissionClient (re)build attempts while the Ray "
        "dashboard is unreachable. Each attempt issues blocking version-check HTTP calls, so this "
        "caps the reconstruction storm to one try per interval; the client still self-heals once Ray "
        "comes up.",
    )
