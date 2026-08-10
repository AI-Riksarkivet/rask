"""The service's SURFACE: the HTTP inbox door and the bus ingress.

Two names, and they are the whole contract with the entrypoint: `routers`, which
`make_service_app` mounts under `RASK_API_PREFIX`, and `register_subscriptions(app)`, which is called
after `app` exists because a Dapr subscription is a route and there is no app to hang one on before
that. Re-exported at the package level rather than named module-by-module, so the layout inside here
can change without touching `notifications/__init__.py`.

The two halves share their middle: `api.ingest` decides what one lineage event means and who is told,
and both the subscription and the `/events` reconciler call it. That is what makes "the same event
arriving on two lanes lands one pointer" a property of the code rather than a coincidence.
"""

from collections.abc import Sequence

from fastapi import APIRouter

from notifications.api import inbox
from notifications.api.subscriptions import register_subscriptions


#: Mounted under the api prefix. One router today; a tuple because the entrypoint splats it.
routers: Sequence[APIRouter] = (inbox.router,)

__all__ = ["register_subscriptions", "routers"]
