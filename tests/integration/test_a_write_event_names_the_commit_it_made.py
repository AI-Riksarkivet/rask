"""A catalog write's lineage event names the commit that write made: its version, its ref, its incarnation.

[[LH-214]]. The WROTE edge is how the graph says which snapshot a run produced, so it must carry the
commit that happened, never a re-read. Three ways the event named some other commit:

* **a reopen for the version.** The ``dir`` backend's ``insert_into_table`` answers ``{}`` on pylance
  12.0.0, so the door reopened the table and stamped whatever was latest — under concurrent appends, the
  other writer's version.
* **a branch write recorded as main.** A branch keeps its own version sequence, so a door that pins the
  branch's number but names no ref (update, delete, the in-pod compact and reindex, schema metadata)
  records a different snapshot.
* **a recreated branch.** It restarts its numbering, so ``(table, ref, N)`` names two commits; pylance's
  ``branch_identifier`` tells the incarnations apart, and the event must carry it.

Driven through the real catalog app on real pylance, and the event is captured on the WIRE: the real
``HttpLineageEmitter`` builds and posts it, and an ``httpx.MockTransport`` stands in for the lineage service.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from lance_namespace import connect

from catalog.core.lineage_emit import HttpLineageEmitter
from catalog.services import dataplane
from service_kit.control_emit import CatalogControlEvent


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "lh214$t"
BRANCH = "dev"


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _rows(*ids: int) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "v": [f"v{i}" for i in ids]})


@pytest.fixture
def events(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every lineage event the catalog posts, as the lineage service would receive it."""
    posted: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _receive(request: httpx.Request) -> httpx.Response:
        import json

        with lock:
            posted.append(json.loads(request.content))
        return httpx.Response(200)

    emitter = HttpLineageEmitter(httpx.AsyncClient(transport=httpx.MockTransport(_receive)), "http://lineage/api/v1/lineage", job_namespace="lance-catalog")
    monkeypatch.setattr(real_ns_client.app.state, "lineage_emitter", emitter, raising=False)
    return posted


@pytest.fixture
def location(real_ns_client: TestClient) -> str:
    assert real_ns_client.post("/v1/namespace/lh214/create", json={}).status_code == 200
    created = real_ns_client.post(f"/v1/table/{TABLE}/create?mode=overwrite", content=_ipc(_rows(1, 2, 3)), headers=ARROW)
    assert created.status_code == 200, created.text
    return str(real_ns_client.post(f"/v1/table/{TABLE}/describe", json={}).json()["table_uri"])


class _Control:
    """Records the control events the catalog announces, with `ControlEmitter`'s whole signature."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append((str(event.action), dict(event.extra)))


def _dataset(event: dict[str, Any]) -> dict[str, Any]:
    """The written dataset: a run event's one output, or a DDL change's ``DatasetEvent`` dataset."""
    return dict(event["outputs"][0] if "outputs" in event else event["dataset"])


def _lance(event: dict[str, Any]) -> dict[str, Any]:
    """The ``lance`` facet: on the run, or on the dataset of a ``DatasetEvent``, which has no run."""
    facets = event["run"]["facets"] if "run" in event else event["dataset"]["facets"]
    return dict(facets["lance"])


def _version(event: dict[str, Any]) -> int | None:
    facet = _dataset(event).get("facets", {}).get("version")
    return int(facet["datasetVersion"]) if facet else None


def _ids(location: str, version: int) -> set[int]:
    return set(lance.dataset(location, version=version).to_table(columns=["id"])["id"].to_pylist())


def _head(location: str) -> lance.LanceDataset:
    return lance.dataset(location).checkout_version((BRANCH, None))


def _identifier(location: str) -> str:
    chain = lance.dataset(location).branches.list()[BRANCH]["branch_identifier"]
    return str(chain[-1][1])


def test_concurrent_appends_each_emit_the_version_that_carries_their_own_rows(real_ns_client: TestClient, location: str, events: list[dict[str, Any]]) -> None:
    """Each append's event names the version whose commit added THAT append's rows, and no other's."""
    markers = list(range(100, 108))
    start = threading.Barrier(len(markers))
    answered: dict[int, int] = {}

    def _append(marker: int) -> None:
        start.wait()
        response = real_ns_client.post(f"/v1/table/{TABLE}/insert", content=_ipc(_rows(marker)), headers=ARROW)
        assert response.status_code == 200, response.text
        answered[marker] = int(response.json()["version"])

    threads = [threading.Thread(target=_append, args=(marker,)) for marker in markers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    inserts = [event for event in events if _lance(event)["operation"] == "insert"]
    emitted = sorted(_version(event) or -1 for event in inserts)
    assert emitted == sorted(answered.values()), f"the events do not name the versions the appends committed: {emitted} vs {answered}"
    for marker, version in answered.items():
        assert _ids(location, version) - _ids(location, version - 1) == {marker}, f"v{version} is not the commit that added row {marker}"


def test_every_branch_door_emits_its_branch_and_that_branchs_version(real_ns_client: TestClient, location: str, events: list[dict[str, Any]]) -> None:
    """Every door keyed on a ``branch`` request field emits that ref, the branch's head version and schema.

    Main is moved ahead of the branch first, so a version number read off main is a different number
    rather than a coincidentally equal one.

    The two compaction lanes (``maintenance/compact`` and ``compaction_commit``) are not driven: their
    evidence gate refuses every branch on pylance 12.0.0, because a branch's data files resolve through
    main's root (flag 16), so no branch compaction reaches its emit.
    """
    client = real_ns_client
    assert client.post(f"/v1/table/{TABLE}/branches/create", json={"name": BRANCH}).status_code == 200
    for ids in ((4,), (5,), (6,)):
        assert client.post(f"/v1/table/{TABLE}/insert", content=_ipc(_rows(*ids)), headers=ARROW).status_code == 200
    _head(location).create_scalar_index("id", index_type="BTREE", name="id_idx")
    restore_to = _head(location).version
    expected_ref = {"ref": BRANCH, "branchIdentifier": _identifier(location)}

    def _nothing() -> None:
        return None

    doors: list[tuple[str, Callable[[], None], Callable[[], httpx.Response]]] = [
        ("insert", _nothing, lambda: client.post(f"/v1/table/{TABLE}/insert?branch={BRANCH}", content=_ipc(_rows(10)), headers=ARROW)),
        (
            "merge_insert",
            _nothing,
            lambda: client.post(
                f"/v1/table/{TABLE}/merge_insert?branch={BRANCH}&on=id&when_matched_update_all=true&when_not_matched_insert_all=true&use_index=false",
                content=_ipc(_rows(10, 11)),
                headers=ARROW,
            ),
        ),
        ("update", _nothing, lambda: client.post(f"/v1/table/{TABLE}/update", json={"branch": BRANCH, "predicate": "id = 10", "updates": [["v", "'up'"]]})),
        ("delete", _nothing, lambda: client.post(f"/v1/table/{TABLE}/delete", json={"branch": BRANCH, "predicate": "id = 11"})),
        (
            "add_columns",
            _nothing,
            lambda: client.post(f"/v1/table/{TABLE}/add_columns", json={"branch": BRANCH, "new_columns": [{"name": "tmp", "expression": "id + 1"}]}),
        ),
        (
            "alter_columns",
            _nothing,
            lambda: client.post(f"/v1/table/{TABLE}/alter_columns", json={"branch": BRANCH, "alterations": [{"path": "tmp", "rename": "kept"}]}),
        ),
        ("drop_columns", _nothing, lambda: client.post(f"/v1/table/{TABLE}/drop_columns", json={"branch": BRANCH, "columns": ["kept"]})),
        (
            "update_field_metadata",
            _nothing,
            lambda: client.post(f"/v1/table/{TABLE}/update_field_metadata", json={"branch": BRANCH, "updates": [{"path": "id", "metadata": {"unit": "n"}}]}),
        ),
        (
            "update_schema_metadata",
            _nothing,
            lambda: client.post(f"/v1/table/{TABLE}/schema_metadata/update", json={"metadata": {"owner": "dev"}, "branch": BRANCH}),
        ),
        ("create_index", _nothing, lambda: client.post(f"/management/v1/table/{TABLE}/maintenance/reindex?branch={BRANCH}", json={"index_name": "id_idx"})),
        ("restore_table", _nothing, lambda: client.post(f"/v1/table/{TABLE}/restore", json={"branch": BRANCH, "version": restore_to})),
    ]

    wrong: list[str] = []
    for operation, prepare, call in doors:
        prepare()
        before = len(events)
        response = call()
        assert response.status_code == 200, f"{operation}: {response.text}"
        emitted = events[before:]
        assert [_lance(event)["operation"] for event in emitted] == [operation], f"{operation}: {[_lance(event) for event in emitted]}"
        event, head = emitted[0], _head(location)
        named = {key: _lance(event).get(key) for key in expected_ref}
        schema = [field["name"] for field in _dataset(event).get("facets", {}).get("schema", {}).get("fields", [])]
        if (named, _version(event), schema) != (expected_ref, head.version, head.schema.names):
            wrong.append(f"{operation}: ref {named} v{_version(event)} {schema}, branch head v{head.version} {head.schema.names}")
    assert not wrong, "branch writes recorded as some other commit:\n" + "\n".join(wrong)


def test_a_recreated_branch_emits_a_different_identifier_for_the_same_version(
    real_ns_client: TestClient, location: str, events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dev@2`` before a delete-and-recreate and ``dev@2`` after it are two commits, and the events say so.

    The ref's own announcements name the same incarnations: the branch created and deleted events, a tag
    pinned on it, and ``list_branches``, so a consumer holding a write event can match it to them.
    """
    client = real_ns_client
    control = _Control()
    monkeypatch.setattr(client.app.state, "control_emitter", control, raising=False)

    def _write_once() -> dict[str, Any]:
        assert client.post(f"/v1/table/{TABLE}/branches/create", json={"name": BRANCH}).status_code == 200
        before = len(events)
        assert client.post(f"/v1/table/{TABLE}/insert?branch={BRANCH}", content=_ipc(_rows(9)), headers=ARROW).status_code == 200
        return _lance(events[before]) | {"version": _version(events[before])}

    first = _write_once()
    assert client.post(f"/v1/table/{TABLE}/branches/delete", json={"name": BRANCH}).status_code == 200
    second = _write_once()
    assert client.post(f"/v1/table/{TABLE}/tags/create", json={"tag": "t1", "branch": BRANCH, "version": 2}).status_code == 200
    listed = client.post(f"/v1/table/{TABLE}/branches/list", json={}).json()["branches"][BRANCH]["metadata"]

    assert (first["ref"], first["version"]) == (second["ref"], second["version"]) == (BRANCH, 2), (first, second)
    assert first.get("branchIdentifier") and second.get("branchIdentifier"), (first, second)
    assert first["branchIdentifier"] != second["branchIdentifier"], "two incarnations of dev@2 carry one identifier"
    announced = [(action, extra.get("branch_identifier")) for action, extra in control.events]
    assert announced == [
        ("table_branch_created", first["branchIdentifier"]),
        ("table_branch_deleted", first["branchIdentifier"]),
        ("table_branch_created", second["branchIdentifier"]),
        ("table_tag_created", second["branchIdentifier"]),
    ], announced
    assert listed.get("branch_identifier") == second["branchIdentifier"], listed


def test_a_branch_lookup_that_walks_past_the_branch_point_finds_nothing_and_raises_nothing(real_ns_client: TestClient, location: str, tmp_path: Path) -> None:
    """A transaction the branch never committed walks down to the branch's first manifest, which panics on
    ``read_transaction`` (pylance 12.0.0). The lookup runs after a committed restore, so it must answer
    "unknown" rather than raise into a 500 that skips the door's idempotency record."""
    assert real_ns_client.post(f"/v1/table/{TABLE}/branches/create", json={"name": BRANCH}).status_code == 200
    assert real_ns_client.post(f"/v1/table/{TABLE}/insert?branch={BRANCH}", content=_ipc(_rows(9)), headers=ARROW).status_code == 200
    ns = connect("dir", {"root": str(tmp_path)})

    assert dataplane.committed_version(ns, {}, ["lh214", "t"], "00000000-0000-0000-0000-000000000000", branch=BRANCH) is None
