"""A request's caller is judged from its headers before FastAPI reads its body (ADR 0203).

FastAPI reads and parses a route's body before it solves any of the route's dependencies (fastapi 0.140.13,
``routing.get_request_handler``), so a door whose bearer only a dependency checks buffers the whole body of a caller
with no token before refusing it: lineage's intake parses a JSON body up to its cap, and an Arrow upload with no token
holds one of the catalog's write seats for as long as it trickles in. :func:`judge_before_the_body` makes every route
that depends on a service's authentication ``gate`` run the service's judge (:func:`caller_judge`) on the request
first: a caller it refuses is answered then, its body never read, and the caller it admits is kept on the request, where
the gate finds it (:func:`admitted_caller`) rather than judging again.

A route is chosen by its dependencies, never by its path, so a door gains the judgement the moment it depends on the
gate, and the probes and the sidecar's routes, which depend on another gate or none, are left as they are.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.security import HTTPBearer
from starlette.types import Receive, Scope, Send

from lakehouse.governed.settings import OidcSettings


#: What judges a request from its headers alone: it raises the gate's own refusal, or returns having kept the caller.
type Judge = Callable[[Request], Awaitable[None]]

#: The `request.state` attribute a judge keeps the caller it admitted in.
_ADMITTED: Final = "admitted_caller"
_BEARER: Final = HTTPBearer(auto_error=False)


def caller_judge[S: OidcSettings](
    *,
    gate: Callable[..., object],
    settings: Callable[[], S],
    authenticated: Callable[[Request, S, str | None], object],
) -> Judge:
    """The judge for a service whose doors depend on ``gate``: with OIDC on, ``authenticated`` run on the request's
    bearer off the event loop, and the caller it returns kept for the gate.

    The judge stands in for ``gate`` ahead of the body, so it resolves both as FastAPI resolves them for the route,
    through the app's ``dependency_overrides``: an override of ``settings`` is the settings the gate is given, and an
    override of ``gate`` is the gate the route runs, which then judges the request itself.
    """

    async def judge(request: Request) -> None:
        overrides = request.app.dependency_overrides
        if gate in overrides:
            return
        resolved: S = overrides.get(settings, settings)()
        if not resolved.oidc_enabled:
            return
        credentials = await _BEARER(request)
        bearer = credentials.credentials if credentials is not None and credentials.credentials else None
        setattr(request.state, _ADMITTED, await run_in_threadpool(authenticated, request, resolved, bearer))

    return judge


def admitted_caller(request: Request) -> tuple[bool, object]:
    """Whether a judge admitted the request's caller before its body was read, and the caller it kept."""
    return hasattr(request.state, _ADMITTED), getattr(request.state, _ADMITTED, None)


def judge_before_the_body(app: FastAPI, *, gate: Callable[..., object], judge: Judge) -> int:
    """Make every route of ``app`` that depends on ``gate``, directly or through another dependency, run ``judge`` on its
    request before FastAPI reads the body; how many routes judge first. Called once every such route is mounted.

    Every route is walked as it is served, an included router's with the dependencies its inclusion adds
    (``iter_route_contexts``): ``app.router.routes`` holds an included router as one opaque entry (fastapi 0.140.13). The
    wrap is the route's ``handle``, which the router calls for a route of its own and for an included one alike; an
    included route builds its handler afresh on each request and never runs ``route.app``. A route already judging first
    is left as it is: routers are module objects, so a module that builds its app again (a reload) mounts the same
    routes, and a second wrap would judge each request twice.
    """
    gated: list[APIRoute] = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and route not in gated and _depends_on(context.dependant, gate):
            gated.append(route)
    for route in gated:
        if not isinstance(route.handle, _JudgedHandle):
            # The route object's own `handle`, which the router looks up on it: an instance attribute shadowing the
            # method is the one hook both dispatch paths share, and it covers a route the moment it depends on the gate,
            # where a `route_class` would have to be named on every one of the services' routers.
            setattr(route, "handle", _JudgedHandle(route, judge))  # noqa: B010 — the type checker refuses an assignment to a method by name
    return len(gated)


def _depends_on(dependant: Dependant, gate: Callable[..., object]) -> bool:
    return any(sub.call is gate or _depends_on(sub, gate) for sub in dependant.dependencies)


class _JudgedHandle:
    """A route's ``handle`` that runs ``judge`` on a request before handling it. A request of a method the route does not
    serve is handled unjudged, so it is still answered 405."""

    def __init__(self, route: APIRoute, judge: Judge) -> None:
        self._methods = route.methods
        self._handle = route.handle
        self._judge = judge

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._methods or scope["method"] in self._methods:
            await self._judge(Request(scope, receive))
        await self._handle(scope, receive, send)


__all__ = ["Judge", "admitted_caller", "caller_judge", "judge_before_the_body"]
