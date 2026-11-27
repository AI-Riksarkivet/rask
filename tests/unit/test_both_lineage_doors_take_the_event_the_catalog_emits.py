"""Both lineage doors take every shape the catalog's builder makes: the bus lane and the HTTP endpoint.

A catalog DDL change is no run that ran, so `build_write_event` returns an OpenLineage **DatasetEvent** for it and a
RunEvent for a write ([[LIN-004]]). The catalog publishes on the bus; the HTTP door is the declared contract for a
producer with no bus user (a pipeline on its own compute, any external OpenLineage producer), and a recorded event
replayed or re-posted reaches it in the shape the catalog built. A door that declared its body as `RunEvent` would
refuse a static event in FastAPI's validation before any handler ran, so both parse through `models.parse_event`.

DRIVEN THROUGH THE REGISTERED ROUTES with the real builder on one side and the real router on the
other. A test that hand-writes the payload proves only that someone can write a valid one; the
defect is that the two sides disagree, and only the real producer's output can show that.
"""

import logging
from collections.abc import Iterator
from typing import Any, Final, cast

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from catalog.core.lineage_emit import build_write_event
from lakehouse.events.bus_delivery import Verdict
from lakehouse.problems.ns_errors import install_problem_handlers
from lineage.api.v1.router import api_router
from lineage.core.metrics import Door
from lineage.models import DatasetEvent, RunEvent
from lineage.services.consumer import handle_cloud_event
from lineage.services.provenance import ProvenanceStore
from tests.open_gate import admits_everything
from tests.provenance_store_double import ProvenanceStoreDouble
from tests.unit.no_uuids import NO_CATALOG_NAMES


_RUN_ID = "b1b2c3d4-0000-4000-8000-000000000001"
_AUTHOR = "CgVhbGljZRIFbG9jYWw"


#: The shape each door's parse files an event under, by the name the parametrization gives it.
_SHAPES: Final = frozendict({"datasets": DatasetEvent, "runs": RunEvent})


def _recorded_as(store: ProvenanceStoreDouble, door: str) -> list[RunEvent | DatasetEvent]:
    """What ``store`` recorded in the shape ``door`` names: the defect is the parse, so the shape is the assertion."""
    return [event for event in store.recorded if isinstance(event, _SHAPES[door])]


def _emitted(operation: str) -> dict[str, Any]:
    """Exactly what the catalog puts on the wire for this operation — the real builder, no stand-in."""
    return build_write_event(
        table_id="gov$raw",
        namespace="lance",
        author=_AUTHOR,
        version=1,
        operation=operation,
        run_id=_RUN_ID,
        event_time="2026-09-24T12:00:00.000000Z",
        job_namespace="lance",
        catalog_impl="dir",
        warehouse_uri="s3://lance-catalog",
    )


@pytest.fixture
def unauthenticated_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FastAPI, ProvenanceStoreDouble]]:
    """The lineage API with auth off — this gate is about the SHAPE the door accepts, not the policy."""
    from lineage.core.config import get_settings

    monkeypatch.setenv("RASK_INSECURE_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.delenv("RASK_FGA_ENABLED", raising=False)
    monkeypatch.delenv("RASK_OIDC_ENABLED", raising=False)
    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(api_router)
    repo = ProvenanceStoreDouble()
    app.state.provenance = repo
    app.state.names = NO_CATALOG_NAMES
    app.state.decider = None
    yield app, repo
    get_settings.cache_clear()


@pytest.mark.parametrize("operation,door", [("create_table", "datasets"), ("insert", "runs")])
def test_the_http_door_accepts_it_and_records_no_signature_with_it(
    unauthenticated_app: tuple[FastAPI, ProvenanceStoreDouble], operation: str, door: str, event_signer: Any
) -> None:
    """The event arrives carrying a `rask_signature` a client wrote for itself. This door holds a bearer token and no signing key,
    so the facet is a claim and not a signature: the event is recorded without it, from whichever bag it sat on."""
    app, repo = unauthenticated_app
    claimed = event_signer("service-catalog").sign(_emitted(operation), on_behalf_of=_AUTHOR)

    response = TestClient(app).post("/api/v1/lineage", json=claimed)

    assert response.status_code == 201, f"{operation} -> {response.status_code}: {response.text[:400]}"
    [recorded] = _recorded_as(repo, door)
    facets = recorded.dataset.facets if isinstance(recorded, DatasetEvent) else recorded.run.facets
    assert "rask_signature" not in facets, f"a signature nobody verified was recorded with the event: {sorted(facets)}"


@pytest.mark.parametrize("operation,door", [("create_table", "datasets"), ("insert", "runs")])
def test_the_bus_door_accepts_it(operation: str, door: str) -> None:
    repo = ProvenanceStoreDouble()
    import asyncio

    # CAST, not a subclass: the fake records which shape arrived, which is the whole assertion, and the real
    # store would need a catalog for a test about the parse.
    verdict = asyncio.run(handle_cloud_event(cast(ProvenanceStore, repo), {"data": _emitted(operation)}, admits_everything, door=Door.SUBSCRIBER))
    assert verdict is Verdict.TAKEN, f"{operation} -> {verdict}"
    assert len(_recorded_as(repo, door)) == 1, f"{operation} was recorded as neither shape: recorded={repo.recorded}"
