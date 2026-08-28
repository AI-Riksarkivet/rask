"""The controlplane's health endpoint — process liveness only (the k8s reachability
check belongs to the projects reader, not this probe).

The shared ``service_kit.health`` router serves the estate-wide ``Liveness`` badge: one probe body,
one handler, one place to change it.
"""

from service_kit.health import make_health_router


router = make_health_router()
