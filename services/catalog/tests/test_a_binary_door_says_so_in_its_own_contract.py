"""A door that answers bytes must say so in the OpenAPI the estate generates clients from.

FastAPI takes the 200 content type from `responses=` on the decorator. The `media_type` passed to the
`Response` object is set at request time and the schema generator never sees it — so a door can answer
`application/vnd.apache.arrow.file` for its whole life while its published contract says
`application/json`, and nothing complains.

THAT CONTRACT IS CONSUMED, not decorative: `docs/catalog-openapi.json` is committed and gated for
drift, and `frontend/packages/api/src/generated/catalog.ts` is generated from it — so a client built
from the catalog's own spec is told to parse Arrow as JSON ([[LH-186]]).
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI

from catalog.api.v1.endpoints import data as door
from service_kit.lakehouse.ns_errors import install_problem_handlers


_JSON = "application/json"


def _declared_media(route_path: str, method: str) -> set[str]:
    """What the generated OpenAPI says this door answers with on 200."""
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(door.router)
    app.include_router(door.management_router)
    operation = app.openapi()["paths"][route_path][method]
    return set(operation.get("responses", {}).get("200", {}).get("content", {}))


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
