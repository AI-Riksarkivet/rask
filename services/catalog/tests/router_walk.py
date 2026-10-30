"""The routes the catalog serves under its router guard, walked off the composed app, and which of them the guard decides.

`catalog.api.fga_deps.authorize` decides a door before its handler runs when the door names an `{id}` under a guarded
resource, or is a batch door read off its body; it authenticates every other route and leaves it to its handler. The
two suites that walk the router split it on that line: `test_an_undeclared_door_is_refused.py` drives the doors it
decides, `test_a_door_the_router_leaves_open_decides_for_itself.py` the rest.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from catalog.api import fga_deps
from catalog.api.v1.router import api_router


#: A path parameter as a route template spells it, `{name}` or `{name:convertor}`.
PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")


def mounted() -> Iterator[tuple[str, APIRoute]]:
    """Every route under the router guard, with the path it is mounted at.

    `iter_route_contexts` rather than `api_router.routes`: this FastAPI keeps included routers as opaque wrappers, and a
    walk over them finds nothing and passes by iterating nothing.
    """
    app = FastAPI()
    app.include_router(api_router)
    for context in iter_route_contexts(app.routes):
        if isinstance(context.original_route, APIRoute) and context.path is not None:
            yield context.path, context.original_route


def names_a_guarded_object(path: str) -> bool:
    """Whether `path` names an `{id}` under a resource `authorize` decides by its rung maps."""
    return "{id}" in path and fga_deps._resource_for(PARAM.sub("x", path)) is not None


def decided_by_the_router(path: str) -> bool:
    """Whether `authorize` decides `path` before its handler runs: a guarded object's door, or a batch door."""
    return names_a_guarded_object(path) or path.rstrip("/") in fga_deps._BATCH_PATHS
