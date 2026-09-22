"""A door that answers bytes must say so in the OpenAPI the estate generates clients from.

FastAPI takes the 200 content type from `responses=` on the decorator. The `media_type` passed to the
`Response` object is set at request time and the schema generator never sees it — so a door can answer
`application/vnd.apache.arrow.file` for its whole life while its published contract says
`application/json`, and nothing complains.

THAT CONTRACT IS CONSUMED, not decorative: `docs/catalog-openapi.json` is committed and gated for
drift, and `frontend/packages/api/src/generated/catalog.ts` is generated from it — so a client built
from the catalog's own spec is told to parse Arrow as JSON ([[LH-186]]).

THE GATE READS BOTH SIDES rather than listing the doors it knows about. The ACTUAL media type comes
from the source (the `media_type=` on whatever Response the handler returns); the DECLARED one comes
from the generated OpenAPI. A door added later is covered without anyone remembering to add it here,
which a hand-maintained list would not do.
"""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest
from fastapi import FastAPI

from catalog.api.v1.endpoints import data as door
from service_kit.lakehouse.ns_errors import install_problem_handlers


_SOURCE = pathlib.Path(door.__file__)
_JSON = "application/json"

#: Module-level names in `data.py` that hold a media type, resolved to their value. A handler passes
#: `media_type=ARROW_FILE`, not a literal, so the AST alone cannot say what it answers.
_MEDIA_NAMES = {name: getattr(door, name) for name in dir(door) if isinstance(getattr(door, name, None), str) and "/" in getattr(door, name)}


def _declared_media(route_path: str, method: str) -> set[str]:
    """What the generated OpenAPI says this door answers with on 200."""
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(door.router)
    app.include_router(door.management_router)
    operation = app.openapi()["paths"][route_path][method]
    return set(operation.get("responses", {}).get("200", {}).get("content", {}))


def _binary_handlers() -> list[tuple[str, str]]:
    """``(function name, media type)`` for every handler that returns a non-JSON Response.

    Read off the source rather than by calling the handlers: the media type is chosen where the
    Response is constructed, and reaching that line means having a dataset, a token and a namespace.
    """
    tree = ast.parse(_SOURCE.read_text())
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Name):
                continue
            if inner.func.id not in {"Response", "StreamingResponse"}:
                continue
            for keyword in inner.keywords:
                if keyword.arg != "media_type":
                    continue
                media = keyword.value.id if isinstance(keyword.value, ast.Name) else None
                resolved = _MEDIA_NAMES.get(media) if media else None
                if resolved and resolved != _JSON:
                    found.append((node.name, resolved))
    return found


def test_the_walk_finds_the_binary_doors() -> None:
    """The gate is only as good as its reading of the module — an empty list would pass everything."""
    assert _binary_handlers(), "no handler in data.py was seen returning a non-JSON Response; the AST walk is reading the wrong thing"


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/v1/table/{id}/query", "post", door.ARROW_FILE),
        ("/management/v1/table/{id}/changes", "post", door.ARROW_FILE),
    ],
)
def test_an_ARROW_door_declares_arrow_and_not_json(path: str, method: str, expected: str) -> None:
    """A generated client dispatches on this. Declaring JSON tells it to parse an Arrow FILE as text."""
    declared = _declared_media(path, method)

    assert expected in declared, f"{method.upper()} {path} answers {expected} but its contract declares {declared or 'nothing'}"
    assert _JSON not in declared, f"{method.upper()} {path} still advertises {_JSON}"


def test_the_BLOB_door_declares_octet_stream() -> None:
    """Same defect, different media type — and the one where a JSON-parsing client corrupts the bytes
    rather than merely failing, since an arbitrary blob may decode as text far enough to look read."""
    declared = _declared_media("/management/v1/table/{id}/blobs", "get")

    assert "application/octet-stream" in declared, f"the blob door declares {declared or 'nothing'}"
    assert _JSON not in declared, f"the blob door still advertises {_JSON}"
