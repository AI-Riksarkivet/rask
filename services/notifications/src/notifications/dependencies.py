"""Service DI — everything a route needs, read off `app.state` where the lifespan put it.

Same shape as `flows.dependencies`: `Annotated[X, Depends(getter)]` aliases, so a route signature names
the resource and nothing else. Reading `app.state` inside a route body instead would make the route
untestable without a full app.
"""

from typing import Annotated

from fastapi import Depends, Request

from notifications.config import NotificationsSettings
from service_kit.exceptions import ServiceUnavailableError


def get_notifications_settings_from_state(request: Request) -> NotificationsSettings:
    return request.app.state.notifications_settings


NotificationsSettingsDep = Annotated[NotificationsSettings, Depends(get_notifications_settings_from_state)]


def require_actor_plane(request: Request) -> None:
    """Refuse the inbox when this process could not register its actor types.

    `app.state.actors_registered` is set False at import and only flipped by the lifespan, and THIS is
    what reads it. The annotator carries the same flag with a comment promising the task endpoints
    surface it as a 503, and nothing consults it — so a registration failure there degrades into
    per-request errors from the sidecar instead of one honest refusal. An inbox route with no actor
    plane cannot answer at all, so it says so.

    What the flag proves is LOCAL and must not be read as more: `register_actor` builds type info and
    stores a manager in a dict, and daprd learns the entity list afterwards by polling `/dapr/config`.
    A sidecar that is absent or not placing actors still reports registered here and still fails every
    invocation — which the proxy surfaces per call, as it must.
    """
    if not getattr(request.app.state, "actors_registered", False):
        raise ServiceUnavailableError("the inbox actor plane is unregistered")


ActorPlaneDep = Annotated[None, Depends(require_actor_plane)]
