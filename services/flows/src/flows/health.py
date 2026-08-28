"""The flows service's health endpoint — process liveness only.

Liveness, deliberately not a readiness that probes Ray Serve: a probe that fails when a MODEL is
unreachable turns "no GPU today" into a restart loop, and a flow service with no Serve behind it can
still serve its catalog, validate a graph, and refuse a model node honestly.

The shared ``service_kit.health`` router serves the estate-wide ``Liveness`` badge — one probe body,
one handler, one place to change it. `make_service_app` supplies no health route; every service mounts
its own, and the chart probes ``/api/health``.
"""

from service_kit.health import make_health_router


router = make_health_router()
