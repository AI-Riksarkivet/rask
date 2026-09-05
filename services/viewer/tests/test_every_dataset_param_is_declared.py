"""Every `?dataset=` on every route must be the DECLARED param, not a bare default (VS-21).

docs/DECISIONS.md "The Python estate audit" VS-21. Eleven route parameters were written `dataset: str | None = None` while
the package they import from already defines the alias:

    DatasetParam = Annotated[str | None, Query(description="Dataset id (default DB when omitted).")]

WHAT THE FINDING PREDICTED IS NOT WHAT THE DOCUMENT SHOWS, and this file says so rather than
pretending otherwise. The described harm — the selector losing its OpenAPI description — does not
reproduce today: every one of the eleven routes also carries a corpus gate
(`REQUIRE_CORPUS_DATA` / `REQUIRE_CORPUS_METADATA`), the gate dependency declares `DatasetParam`
itself, and FastAPI merges the two declarations of the same query parameter — so the description
arrives from the gate. The duplication is real; its consequence was masked.

So the gate below is on the SOURCE, which is where the defect actually lives, and the contract
assertions state the property the source rule protects: they hold today via the gates, and would
start failing the day a route is written without one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from viewer.api.v1 import endpoints
from viewer.api.v1.router import router as viewer_router


ENDPOINTS_DIR = Path(endpoints.__file__).parent

#: The bare declaration the alias replaces — on its own line or inline in a one-line signature.
_BARE = "dataset: str | None = None"


def _dataset_params() -> list[tuple[str, str, dict[str, Any]]]:
    app = FastAPI()
    app.include_router(viewer_router)
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path, operations in app.openapi()["paths"].items():
        for method, op in operations.items():
            for param in op.get("parameters", []):
                if param.get("name") == "dataset":
                    found.append((method, path, param))
    return found


def test_no_route_declares_the_selector_by_hand() -> None:
    offenders = {module.name: module.read_text().count(_BARE) for module in ENDPOINTS_DIR.glob("*.py") if _BARE in module.read_text()}
    assert not offenders, (
        f"{offenders} declare `dataset: str | None = None` instead of the `DatasetParam` alias — the same selector, described in one place and undescribed in another"
    )


def test_the_service_still_offers_the_selector() -> None:
    """A guard on the guard: an empty list would make the assertions below vacuous."""
    assert len(_dataset_params()) >= 11


def test_every_dataset_param_carries_its_description() -> None:
    undescribed = [f"{m.upper()} {p}" for m, p, param in _dataset_params() if not param.get("description")]
    assert not undescribed, f"{undescribed} publish `?dataset=` with no description"


def test_every_dataset_param_is_an_optional_query_string() -> None:
    """One selector, one shape: optional, in the query string, defaulting to the default dataset."""
    wrong = [f"{m.upper()} {p}" for m, p, param in _dataset_params() if param.get("in") != "query" or param.get("required")]
    assert not wrong, wrong
