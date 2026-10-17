"""A pipeline's run, posted with its own token, reaches notifications and the configured graph backend (LH-468, ADR 0158).

Notifications hears of a run posted over HTTP only through the feed's projection, which its reconciler follows
forward (the HTTP door records; it publishes nothing on the bus), so the run must be on `GET /events/projection` with
the author the token verified. The lineage graph is bring-your-own: the relay tick forwards every event lineage
recorded, as standard OpenLineage, to the backend `LINEAGE_FORWARD_URL` names, over the OpenLineage HTTP transport.
Delivery is at least once and in feed order: a backend that is down holds the forwarder's cursor where it was, and
the next tick sends the event once the backend answers. The cursor is kept in the catalog, so a tick after a delivery
sends nothing again.

Driven over real sockets: lineage's router behind its real authentication gate records into a live catalog, its
relay tick runs through the real route behind the sidecar-token guard, and the backend is a live OpenLineage
endpoint that records what it is sent.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from tests.integration.lineage_app import RELAY, SUBJECT_HEADER, GraphBackend, governed_lineage, graph_backend, live_catalog, run_event


_TABLE = "acme-silver$pages"
_SIDECAR = {"dapr-api-token": "s3cret"}
_ALICE = "dex~alice"
#: The notifications service, which reads the event feed under its own service account.
_NOTIFICATIONS = "kubernetes~system:serviceaccount:rask:rask-sa-notifications"


@pytest.fixture
def backend() -> Generator[tuple[GraphBackend, str]]:
    with graph_backend() as served:
        yield served


def test_a_run_reaches_the_backend_once_across_an_outage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: tuple[GraphBackend, str]) -> None:
    graph, url = backend
    monkeypatch.setenv("LINEAGE_FORWARD_URL", url)
    monkeypatch.setenv("APP_API_TOKEN", _SIDECAR["dapr-api-token"])
    granted = {(_ALICE, "can_write_data", f"table:{_TABLE}"), (_NOTIFICATIONS, "can_read_event_feed", "estate:rask")}
    run = run_event("1d3c5a07-0000-4000-8000-000000000592", "COMPLETE", "2026-10-15T10:00:00+00:00", outputs=[_TABLE])
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, governed_lineage(catalog_url, monkeypatch, granted) as lineage:
        posted = lineage.post("/api/v1/lineage", json=run, headers={SUBJECT_HEADER: _ALICE})
        projected = lineage.get("/events/projection", params={"since": 0}, headers={SUBJECT_HEADER: _NOTIFICATIONS}).json()
        graph.down = True
        while_down = lineage.post(f"/{RELAY}", headers=_SIDECAR).json()
        graph.down = False
        once_up = lineage.post(f"/{RELAY}", headers=_SIDECAR).json()
        after = lineage.post(f"/{RELAY}", headers=_SIDECAR).json()

    assert posted.status_code == 201, posted.text
    assert [(row["event"]["run"]["runId"], row["event"]["run"]["facets"]["author"]["sub"]) for row in projected["events"]] == [(run["run"]["runId"], _ALICE)]
    assert [(tick["forwarded"], tick["forward_stalled"]) for tick in (while_down, once_up, after)] == [(0, "HTTP 503"), (1, None), (0, None)]
    assert [(event["run"]["runId"], event["eventType"], event["run"]["facets"]["author"]["sub"]) for event in graph.received] == [
        (run["run"]["runId"], "COMPLETE", _ALICE)
    ]
