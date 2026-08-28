"""The frontend-facing health badge — process liveness only.

Deliberately not a readiness that probes the sidecar or the state store: this is the path the chart's
default `healthPath` (`/api/health`) points at, and a probe that fails when a DEPENDENCY is briefly
unreachable turns a blip into a restart loop. The operational pair (`/livez` + `/readyz`, root-mounted
so a supervisor need not know this service's api prefix) is where per-component reporting lives.

The shared ``service_kit.health`` router serves the estate-wide ``Liveness`` badge — one probe body,
one handler, one place to change it.
"""

from service_kit.health import make_health_router


router = make_health_router()
