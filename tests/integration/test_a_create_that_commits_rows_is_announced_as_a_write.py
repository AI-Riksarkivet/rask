"""A create that commits rows is announced as a write; a create that commits none stays a dataset change ([[LH-492]]).

A table's first version can hold rows: the catalog's own `create` door writes the Arrow body it is sent, and a stock
client's `write_dataset(mode="create")` stages v1 and finalizes it through `version/create` (ADR 0157). The version a
write made is carried only by a run's output (`docs/COMMIT-EVENTS.md`), so a create announced as a `DatasetEvent`, which
names no run, hides v1's rows from a follower acting on writes and leaves v1 with no author's write in lineage.

Driven over real sockets, as `test_the_commit_feed_is_a_lance_table_followed_forward.py` is: the real catalog app on a
real `dir` namespace announces each commit to lineage's own app, which records it in the feed this test follows. The
verified caller is a stand-in (`authenticate` answers the person `dex~alice`), because who is signed in is not the claim; that the
announcement carries the caller as its author is.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import lance
import lance_namespace
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from tests.integration.lineage_app import free_port, live_catalog, live_lineage


_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
_NAMESPACE = "acme-silver"
_TABLE = f"{_NAMESPACE}$pages"


def _rows(count: int) -> pa.Table:
    return pa.table({"id": pa.array(range(count), pa.int64())})


def _through_the_create_door(catalog_url: str, rows: pa.Table) -> None:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    created = httpx.post(f"{catalog_url}/v1/table/{_TABLE}/create", content=sink.getvalue().to_pybytes(), headers=_ARROW, timeout=60)
    assert created.status_code == 200, created.text


def _through_a_stock_client(catalog_url: str, rows: pa.Table) -> None:
    stock = lance_namespace.connect("rest", {"uri": catalog_url})
    lance.write_dataset(rows, namespace_client=stock, table_id=[_NAMESPACE, "pages"], mode="create", data_storage_version="2.2", enable_stable_row_ids=True)


DOORS: dict[str, Callable[[str, pa.Table], None]] = {"create": _through_the_create_door, "version/create": _through_a_stock_client}


@contextmanager
def _signed_in(key: str) -> Generator[None]:
    """Every request to the catalog authenticated as the person whose principal key is ``key``."""
    from catalog.api.security import authenticate
    from catalog.main import app

    principal = Principal.parse(key)
    claims = IDToken(iss="https://idp.invalid/dex", sub=principal.subject, aud="rask", exp=4102444800, iat=0)
    app.dependency_overrides[authenticate] = lambda: PersonPrincipal(principal=principal, claims=claims)
    try:
        yield
    finally:
        app.dependency_overrides.pop(authenticate, None)


def _feed(lineage_url: str) -> list[dict[str, Any]]:
    """Every row the feed holds, followed forward from its start."""
    seen: list[dict[str, Any]] = []
    cursor = 0
    with httpx.Client(base_url=lineage_url, timeout=60) as lineage:
        while True:
            page = lineage.get("/events", params={"since": cursor, "limit": 500}).json()
            seen += page["events"]
            if page["next_cursor"] == cursor:
                return seen
            cursor = page["next_cursor"]


def _facets(event: dict[str, Any]) -> dict[str, Any]:
    """Where the catalog's `lance` and `author` facets sit: on the run, or on the dataset of a dataset change."""
    return event["run"]["facets"] if "run" in event else event["dataset"]["facets"]


@pytest.mark.parametrize("door", list(DOORS))
@pytest.mark.parametrize("count", [2, 0], ids=["rows", "no-rows"])
def test_a_create_that_commits_rows_is_announced_as_a_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str, count: int) -> None:
    port = free_port()
    announced = {"LANCE_LINEAGE_EMIT_ENABLED": "true", "LANCE_LINEAGE_TRANSPORT": "http", "LANCE_LINEAGE_URL": f"http://127.0.0.1:{port}/api/v1/lineage"}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **announced) as catalog_url,
        live_lineage(catalog_url, monkeypatch, port=port) as lineage_url,
        _signed_in("dex~alice"),
    ):
        assert httpx.post(f"{catalog_url}/v1/namespace/{_NAMESPACE}/create", json={}, timeout=60).status_code == 200
        DOORS[door](catalog_url, _rows(count))
        creates = [row for row in _feed(lineage_url) if _facets(row["event"])["lance"]["operation"] == "create_table"]

    assert len(creates) == 1, f"{door}: the create must appear once in the feed, got {creates}"
    row, event = creates[0], creates[0]["event"]
    assert _facets(event)["author"]["sub"] == "dex~alice", f"{door}: the create is not the verified caller's: {_facets(event)}"
    if count == 0:
        assert row["job"] is None and "run" not in event, f"{door}: a create that committed no rows ran no job: {event}"
        return
    assert row["job"] is not None and "run" in event, f"{door}: a create that committed rows was announced as a dataset change: {event}"
    assert event["eventType"] == "COMPLETE"
    (output,) = event["outputs"]
    assert output["name"] == _TABLE
    assert output["facets"]["version"]["datasetVersion"] == "1", f"{door}: the write does not name the version it made: {output['facets']}"
    assert output["facets"]["lifecycleStateChange"]["lifecycleStateChange"] == "CREATE", output["facets"]
