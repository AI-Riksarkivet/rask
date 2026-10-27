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
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from lance.dataset import Branches
from lance_namespace import connect

from catalog.core.lineage_emit import HttpLineageEmitter
from catalog.services import dataplane
from lineage.models import parse_event
from lineage.services.provenance import edges_of
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
        self.named: list[tuple[str, str | None, str | None]] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append((str(event.action), dict(event.extra)))
        self.named.append((str(event.action), event.object_uuid, event.namespace_uuid))


def _dataset(event: dict[str, Any]) -> dict[str, Any]:
    """The written dataset: a run event's one output, or a DDL change's ``DatasetEvent`` dataset."""
    return dict(event["outputs"][0] if "outputs" in event else event["dataset"])


def _lance(event: dict[str, Any]) -> dict[str, Any]:
    """The ``rask_lance`` facet: on the run, or on the dataset of a ``DatasetEvent``, which has no run."""
    facets = event["run"]["facets"] if "run" in event else event["dataset"]["facets"]
    return dict(facets["rask_lance"])


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
    """Every door keyed on a ``branch`` request field emits that ref, the branch's head version and schema, and commits
    that version naming its verified caller and the door's own operation ([[LH-497]]).

    Main is moved ahead of the branch first, so a version number read off main is a different number
    rather than a coincidentally equal one.
    """
    from catalog.api.security import CurrentToken
    from service_kit.governed.idp_identity import PersonPrincipal
    from service_kit.governed.oidc import IDToken
    from service_kit.governed.principal import Principal

    caller = PersonPrincipal(principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=4102444800, iat=1))
    real_ns_client.app.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: caller
    client = real_ns_client
    assert client.post(f"/v1/table/{TABLE}/branches/create", json={"name": BRANCH}).status_code == 200
    for ids in ((4,), (5,), (6,)):
        assert client.post(f"/v1/table/{TABLE}/insert", content=_ipc(_rows(*ids)), headers=ARROW).status_code == 200
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
        committed = head.read_transaction(head.version)
        stamp = dict(committed.transaction_properties or {}) if committed is not None else {}
        if stamp != {"rask.author": "dex~alice", "rask.operation": operation}:
            wrong.append(f"{operation}: the branch head v{head.version} committed {stamp}")
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

    from catalog.core.config import get_settings
    from catalog.services import table_claims

    table = table_claims.door_table(ns, table_claims.claim_store(get_settings()), ["lh214", "t"])
    assert dataplane.committed_version(table, {}, "00000000-0000-0000-0000-000000000000", branch=BRANCH) is None


def test_every_event_names_the_table_and_its_namespace_by_their_uuids(
    real_ns_client: TestClient, events: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write, a rename, a write and a definition change under the new name, and a drop each announce the table and its
    namespace by their uuids beside their names, on the lineage event and the control event; lineage's projection keeps
    them, and the file's ``lineage.dataset_id`` is the table's uuid ([[LH-522]], ADR 0173 §10).

    The drop's event is built after the table is gone, so it names what the door resolved before it acted.
    """
    from lineage.api.v1.endpoints.events import targeting_view

    client = real_ns_client
    control = _Control()
    monkeypatch.setattr(client.app.state, "control_emitter", control, raising=False)
    assert client.post("/v1/namespace/lh522/create", json={}).status_code == 200
    assert client.post("/v1/table/lh522$t/create", content=_ipc(_rows(1)), headers=ARROW).status_code == 200
    described = client.post("/v1/table/lh522$t/describe", json={}).json()
    table_uuid = described["properties"]["rask.uuid"]
    namespace_uuid = client.post("/v1/namespace/lh522/describe", json={}).json()["properties"]["rask.uuid"]
    stamped = (lance.dataset(described["location"]).schema.metadata or {}).get(b"lineage.dataset_id")
    for call in (
        lambda: client.post("/v1/table/lh522$t/insert", content=_ipc(_rows(2)), headers=ARROW),
        lambda: client.post("/v1/table/lh522$t/rename", json={"new_table_name": "u"}),
        lambda: client.post("/v1/table/lh522$u/insert", content=_ipc(_rows(3)), headers=ARROW),
        lambda: client.post("/v1/table/lh522$u/add_columns", json={"new_columns": [{"name": "w", "expression": "id + 1"}]}),
        lambda: client.post("/v1/table/lh522$u/drop", json={}),
    ):
        assert call().status_code == 200

    uuids = {"tableUuid": table_uuid, "namespaceUuid": namespace_uuid}

    def _named(dataset: dict[str, Any]) -> dict[str, Any]:
        return {key: dataset.get("facets", {}).get("rask_lance", {}).get(key) for key in uuids}

    announced = [(_lance(event)["operation"], _named(_dataset(event))) for event in events]
    assert announced == [
        (operation, uuids) for operation in ("create_table", "insert", "deregister_table", "register_table", "insert", "add_columns", "drop_table")
    ], announced
    assert [_named(source) for source in events[3]["inputs"]] == [uuids], "the rename's source is not named by the table's uuid"
    projected = [_named(_dataset(targeting_view(event))) for event in events]
    assert projected == [uuids] * len(events), projected
    assert [named for named in control.named if named[0].startswith("table_")] == [
        ("table_created", table_uuid, namespace_uuid),
        ("table_renamed", table_uuid, namespace_uuid),
        ("table_dropped", table_uuid, namespace_uuid),
    ], control.named
    assert stamped == table_uuid.encode(), stamped


def _incarnations(location: str) -> dict[str, str]:
    """Each branch's incarnation as pylance records it: the last uuid of its ``branch_identifier`` chain."""
    return {name: str(entry["branch_identifier"][-1][1]) for name, entry in lance.dataset(location).branches.list().items()}


@pytest.mark.parametrize(
    ("pinned_meanwhile", "status", "removed"),
    [
        pytest.param(False, 200, ["a/b", "a"], id="nested-ones-first"),
        # A tag on `a` landing after the door's check and after `a/b` went: the delete stops at `a`, and `a/b` is gone.
        pytest.param(True, 409, ["a/b"], id="a-pin-since-the-check-stops-it-part-way"),
    ],
)
def test_a_branch_delete_announces_every_branch_it_removes_as_the_incarnation_it_was(
    real_ns_client: TestClient, location: str, monkeypatch: pytest.MonkeyPatch, pinned_meanwhile: bool, status: int, removed: list[str]
) -> None:
    """[[LH-413]] Deleting ``a`` removes ``a/b`` with it ([[LH-203]]), so a console holding the branch list hears both, and
    hears what went even when a pin made since the check stops the delete part-way."""
    client = real_ns_client
    control = _Control()
    monkeypatch.setattr(client.app.state, "control_emitter", control, raising=False)
    # Planted through pylance: the create door refuses a name nesting with another, so only a table made before that
    # refusal carries the pair.
    lance.dataset(location).create_branch("a")
    lance.dataset(location).create_branch("a/b")
    incarnations = _incarnations(location)
    if pinned_meanwhile:
        delete = Branches.delete

        def _delete_then_pin(self: Branches, branch: str) -> None:
            delete(self, branch)
            if branch == "a/b":
                lance.dataset(location).tags.create("pin", ("a", None))

        monkeypatch.setattr(Branches, "delete", _delete_then_pin)

    answer = client.post(f"/v1/table/{TABLE}/branches/delete", json={"name": "a"})

    assert answer.status_code == status, answer.text
    assert [(action, extra["branch"], extra.get("branch_identifier")) for action, extra in control.events] == [
        ("table_branch_deleted", name, incarnations[name]) for name in removed
    ], control.events


def test_an_edge_names_the_incarnation_of_each_branch_it_joins(real_ns_client: TestClient, location: str, events: list[dict[str, Any]]) -> None:
    """[[LH-413]] An edge reads ``src@feed`` into ``t@dev``; each ref is named with its incarnation, since a branch deleted
    and created again under the name restarts its numbering. The event the catalog emits is the one lineage records."""
    client = real_ns_client
    created = client.post("/v1/table/lh214$src/create?mode=create", content=_ipc(_rows(7)), headers=ARROW)
    assert created.status_code == 200, created.text
    for table, branch in (("lh214$src", "feed"), (TABLE, BRANCH)):
        assert client.post(f"/v1/table/{table}/branches/create", json={"name": branch}).status_code == 200
    run = {"x-rask-run-id": "run-413", "x-rask-inputs": json.dumps([{"table": ["lh214", "src"], "version": 1, "branch": "feed"}])}
    before = len(events)

    answer = client.post(f"/v1/table/{TABLE}/insert?branch={BRANCH}", content=_ipc(_rows(9)), headers={**ARROW, **run})

    assert answer.status_code == 200, answer.text
    (written,) = events[before:]
    edges = edges_of(parse_event(written), received_at=datetime.now(UTC))
    identifiers = [(edge["in_branch"], edge["in_branch_identifier"], edge["out_branch"], edge["out_branch_identifier"]) for edge in edges]
    source, target = _incarnations(str(created.json()["location"])), _incarnations(location)
    assert identifiers == [("feed", source["feed"], BRANCH, target[BRANCH])], identifiers
