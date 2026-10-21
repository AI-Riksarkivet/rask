"""Whatever the catalog emits, BOTH lineage doors must accept — the bus and the HTTP endpoint.

[[LIN-004]] (2026-09-21) moved a catalog DDL change off the run shape: a create is no run that ran, so
`build_write_event` returns an OpenLineage **DatasetEvent** for it. The bus door was taught the new shape
first, and the HTTP door, declaring its body as `RunEvent` (which requires `eventType`, `run` and `job`),
refused a static event in FastAPI's validation before any handler ran. Both doors now parse through
`models.parse_event`, and the store records whichever shape arrives.

MEASURED 2026-09-24 against the hermetic governance stack — the catalog on the HTTP transport, which
is its default:

    POST http://lineage-api:8000/api/v1/lineage "HTTP/1.1 422 Unprocessable Content"
    lineage_emit_failed operation='create_table' table='gov32$bronze'

and lineage's durable feed answered `{"events":[],"next_cursor":null,"oldest_seq":null}` — not one
event, for three creates. The emit is BEST-EFFORT by design, so every catalog create over HTTP lost
its provenance silently and the only symptom was a governance assertion reading
`expected lineage creator=..., got None`.

THE DEPLOYED ESTATE IS ON THE BUS (`chart/templates/services.yaml` sets
`LANCE_LINEAGE_TRANSPORT=dapr`), so this is not a production outage — but the HTTP door is a
DECLARED contract, not a dev convenience. Its own source calls it "the ONLY door for a producer with
no Dapr sidecar — the whole Ray lane, every runner, and any external OpenLineage producer".

DRIVEN THROUGH THE REGISTERED ROUTES with the real builder on one side and the real router on the
other. A test that hand-writes the payload proves only that someone can write a valid one; the
defect is that the two sides disagree, and only the real producer's output can show that.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from catalog.core.lineage_emit import build_write_event
from lineage.api.v1.router import api_router
from lineage.core.metrics import Door
from lineage.models import DatasetEvent, RunEvent
from lineage.services.consumer import handle_cloud_event
from lineage.services.provenance import ProvenanceStore
from service_kit.lakehouse.ns_errors import install_problem_handlers
from tests.unit.no_uuids import NO_CATALOG_NAMES


_RUN_ID = "b1b2c3d4-0000-4000-8000-000000000001"
_AUTHOR = "CgVhbGljZRIFbG9jYWw"


class _Repo:
    """Files what it records by the shape the door parsed it into, because the defect is the parse."""

    def __init__(self) -> None:
        self.runs: list[RunEvent] = []
        self.datasets: list[DatasetEvent] = []

    async def record(self, event: RunEvent | DatasetEvent) -> None:
        if isinstance(event, DatasetEvent):
            self.datasets.append(event)
        else:
            self.runs.append(event)


def _emitted(operation: str) -> dict[str, Any]:
    """Exactly what the catalog puts on the wire for this operation — the real builder, no stand-in."""
    return build_write_event(
        table_id="gov$bronze",
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
def unauthenticated_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FastAPI, _Repo]]:
    """The lineage API with auth off — this gate is about the SHAPE the door accepts, not the policy."""
    from lineage.core.config import get_settings

    monkeypatch.setenv("RASK_INSECURE_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.delenv("RASK_FGA_ENABLED", raising=False)
    monkeypatch.delenv("RASK_OIDC_ENABLED", raising=False)
    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(api_router)
    repo = _Repo()
    app.state.provenance = repo
    app.state.names = NO_CATALOG_NAMES
    yield app, repo
    get_settings.cache_clear()


@pytest.mark.parametrize("operation,door", [("create_table", "datasets"), ("insert", "runs")])
def test_the_http_door_accepts_it_and_records_no_signature_with_it(
    unauthenticated_app: tuple[FastAPI, _Repo], operation: str, door: str, event_signer: Any
) -> None:
    """The event arrives carrying a `rask_signature` a client wrote for itself. This door holds a bearer token and no signing key,
    so the facet is a claim and not a signature: the event is recorded without it, from whichever bag it sat on."""
    app, repo = unauthenticated_app
    claimed = event_signer("service-catalog").sign(_emitted(operation), on_behalf_of=_AUTHOR)

    response = TestClient(app).post("/api/v1/lineage", json=claimed)

    assert response.status_code == 201, f"{operation} -> {response.status_code}: {response.text[:400]}"
    [recorded] = getattr(repo, door)
    facets = recorded.dataset.facets if door == "datasets" else recorded.run.facets
    assert "rask_signature" not in facets, f"a signature nobody verified was recorded with the event: {sorted(facets)}"


@pytest.mark.parametrize("operation,door", [("create_table", "datasets"), ("insert", "runs")])
def test_the_bus_door_accepts_it(operation: str, door: str) -> None:
    repo = _Repo()
    import asyncio

    # CAST, not a subclass: the fake records which shape arrived, which is the whole assertion, and the real
    # store would need a catalog for a test about the parse.
    status = asyncio.run(handle_cloud_event(cast(ProvenanceStore, repo), {"data": _emitted(operation)}, door=Door.SUBSCRIBER))
    assert status == {"status": "SUCCESS"}, f"{operation} -> {status}"
    assert len(getattr(repo, door)) == 1, f"{operation} was recorded as neither shape: runs={repo.runs} datasets={repo.datasets}"
