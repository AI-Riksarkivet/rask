"""The ingest service's health endpoint — process liveness only.

Added 2026-08-03 after the FIRST in-cluster deploy (A11) found the pod stuck at 1/2 forever:
`make_service_app` does NOT supply a health route, every service mounts its own, and this app passed
only the ingest router. The chart probes `/api/health` (fleet.yaml), so the probe 404'd, readiness
never went true, and the Service had no endpoints — a Deployment that looks deployed and serves
nothing.

Worth stating because it is the exact class of defect the estate keeps paying for: nothing about the
image, the render, or the unit suite was wrong. `dagger call image` built it, the app constructed
inside it, `helm template` rendered it, and `kubectl apply --dry-run=server` accepted it. Only an
actual rollout could say the probe path and the app disagreed.

Liveness ONLY — deliberately not a readiness that probes NATS or the catalog. A liveness probe that
fails when a dependency is down turns one broken dependency into a restart loop across every pod that
touches it, which is how a partial outage becomes a total one. The shared ``service_kit.health`` router
serves the estate-wide ``Liveness`` badge: one probe body, one handler, one place to change it.
"""

from service_kit.health import make_health_router


router = make_health_router()
