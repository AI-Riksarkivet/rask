"""The compute service's health endpoint — process liveness (Ray reachability is
`/ray/health`'s job, not this one's).

The shared ``service_kit.health`` router serves the estate-wide ``Liveness`` badge: one probe body,
one handler, one place to change it.
"""

from service_kit.health import make_health_router


router = make_health_router()
