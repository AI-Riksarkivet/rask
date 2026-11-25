"""The path a request's route was matched on, for every gate that decides by path ([[LH-296]]).

A server that mounts an app under a prefix (uvicorn's ``--root-path``) hands each request ``scope["path"]`` with the
prefix on and names the prefix in ``scope["root_path"]``; routing matches with it taken off. A gate that read the raw
path would then recognise no mount and no door: measured 2026-11-17 on the catalog under ``root_path="/api/catalog"``,
its authorization guard let a caller holding nothing count the rows of a table it could not see. So a gate reads the
path through the function routing itself matches with, and a prefix changes neither.

It is the raw ASGI path with the prefix off, never ``request.url.path``: that re-parses the decoded path as a URL and
ends it at a ``?`` or ``#`` inside a name ([[LH-526]]).
"""

from starlette._utils import get_route_path
from starlette.types import Scope


def route_path(scope: Scope) -> str:
    """The path of ``scope`` as routing matches it: ``scope["path"]`` with the server's ``root_path`` taken off."""
    return get_route_path(scope)
