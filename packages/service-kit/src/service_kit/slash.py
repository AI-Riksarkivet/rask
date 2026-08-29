"""Trailing-slash tolerance without redirects.

Dapr service invocation strips a request's trailing slash, so a call to
`/api/v1/batches/` reaches the service as `/api/v1/batches`. FastAPI's default
`redirect_slashes` would answer that with a 307 whose absolute `Location` leaks
the service's in-pod address. Instead we resolve the slash variant in-process:
if the incoming path matches no route but the toggled variant does, rewrite the
ASGI scope path and continue — no redirect, no leak.
"""

from collections.abc import Callable, Sequence

from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Receive, Scope, Send


class SlashToleranceMiddleware:
    def __init__(self, app: ASGIApp, routes_provider: Callable[[], Sequence[BaseRoute]]) -> None:
        self.app = app
        self.routes_provider = routes_provider

    def _path_matches(self, scope: Scope, path: str) -> bool:
        probe = {**scope, "path": path}
        for route in self.routes_provider():
            match, _ = route.matches(probe)
            if match is not Match.NONE:  # FULL or PARTIAL (path matched)
                return True
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path: str = scope["path"]
            if not self._path_matches(scope, path):
                alt = path[:-1] if path.endswith("/") and len(path) > 1 else path + "/"
                if self._path_matches(scope, alt):
                    scope = {**scope, "path": alt}
        await self.app(scope, receive, send)
