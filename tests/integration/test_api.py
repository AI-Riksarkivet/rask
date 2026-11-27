"""Integration tests for *our* REST layer.

The namespace backend is faked, so every assertion is about code we wrote:
identifier parsing from the path, routing to the correct operation, request
assembly (query params / headers / body), response serialization and
content-types, and error → HTTP / Problem-Details mapping. No lance operations
are exercised.
"""

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import (
    CreateNamespaceResponse,
    DescribeTableResponse,
    DescribeTableVersionResponse,
    LanceNamespace,
    ListTableVersionsResponse,
    TableAlreadyExistsError,
    TableVersion,
    TableVersionNotFoundError,
)

from catalog.services import table_bases
from catalog.services.tables.versions import CommitReadback
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.lancekit.features import DATA_STORAGE_VERSION_KEY, STABLE_ROW_IDS_KEY
from lakehouse.naming import UUID_PROPERTY
from lakehouse.records.location_claims import ClaimStore
from tests import seeding
from tests.app_state import app_of
from tests.callers import person
from tests.catalog_settings import catalog_settings


ARROW_STREAM = {"content-type": ARROW_STREAM_MEDIA_TYPE}


# --- identifier parsing (our logic) ---------------------------------------- #


def test_schema_metadata_flat_map_keeps_keys_named_like_envelope_fields(real_ns_client: TestClient) -> None:
    # CONTRACT (audit 2026-07-15): a FLAT body IS the metadata map — a user key literally named "id"
    # (or identity/context) is data, and must land on the table, never be eaten as an envelope field.
    _ensure_namespace(real_ns_client, "lance_catalog$db1")
    assert real_ns_client.post("/v1/table/lance_catalog$db1$users/create", content=_ONE_ROW, headers=ARROW_STREAM).status_code == 200

    resp = real_ns_client.post("/v1/table/lance_catalog$db1$users/schema_metadata/update", json={"id": "row-key", "owner": "alice"})

    assert resp.status_code == 200, resp.text
    location = real_ns_client.post("/v1/table/lance_catalog$db1$users/describe", json={}).json()["location"]
    assert {key: lance.dataset(location).schema.metadata[key.encode()].decode() for key in ("id", "owner")} == {"id": "row-key", "owner": "alice"}


def test_create_namespace_routes_with_body_and_id(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.create_namespace.return_value = CreateNamespaceResponse(properties={"team": "ml"})
    resp = client.post("/v1/namespace/lance_catalog$parent$child/create", json={"properties": {"team": "ml"}})
    assert resp.status_code == 200
    req = fake_ns.create_namespace.call_args.args[0]
    assert req.id == ["lance_catalog", "parent", "child"]
    # Beside the uuid the door mints for the namespace ([[LH-519]]).
    assert {key: value for key, value in req.properties.items() if key != UUID_PROPERTY} == {"team": "ml"}
    assert resp.json() == {"properties": {"team": "ml"}}  # our serialization


# --- query-param / header assembly (our logic) ----------------------------- #


def _ensure_namespace(client: TestClient, name: str) -> None:
    """Create the parent namespace before creating a table under it.

    Required since pylance 9.0: the `dir` backend answers a child-namespace read with
    ``NamespaceNotFoundError: Child namespace reads require an existing __manifest dataset``, where 8.0
    treated any directory as a namespace. So a table create under an undeclared namespace is now a 404
    rather than an implicit mkdir — which is the stricter and more honest contract, and the one the
    product already follows (the namespace-create endpoint and the medallion bootstrap both declare
    theirs). These tests were relying on the implicit behaviour.
    """
    assert client.post(f"/v1/namespace/{name}/create", json={}).status_code == 200


def test_create_table_passes_arrow_bytes_through(real_ns_client: TestClient) -> None:
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    # The raw Arrow-IPC body is written verbatim as the new table's first version. Driven against a REAL
    # dir namespace (a MagicMock can no longer stand in — the create does a real 2.2 write), so this now
    # proves the ACTUAL round-trip: the rows land, not merely that bytes reached a mocked method.
    body = encode_arrow_stream(pa.table({"id": [1, 2, 3]}))
    resp = real_ns_client.post("/v1/table/lance_catalog$db$t/create?mode=overwrite", content=body, headers=ARROW_STREAM)
    assert resp.status_code == 200

    described = real_ns_client.post("/v1/table/lance_catalog$db$t/describe", json={"load_detailed_metadata": True})
    assert described.status_code == 200
    ds = lance.dataset(described.json()["location"])
    assert ds.count_rows() == 3
    assert ds.data_storage_version == "2.2"  # the whole point of routing every create through the direct path
    assert ds.has_stable_row_ids


def test_a_register_the_catalog_cannot_judge_for_a_fault_of_its_own_is_no_outage(
    real_ns_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The register door refuses a dataset it could not judge. A store that did not answer is the retry a caller is
    asked for (17); a fault in the judging itself is the catalog's (18), and no retry clears it."""
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    lance.write_dataset(pa.table({"id": [1]}), str(tmp_path / "judged"), data_storage_version="2.2", enable_stable_row_ids=True)

    def faulty(*_args: object, **_kwargs: object) -> None:
        raise TypeError("a defect in the judge")

    monkeypatch.setattr(table_bases, "registered_dataset_facts", faulty)
    as_a_caller_sees_it = TestClient(real_ns_client.app, raise_server_exceptions=False)
    as_a_caller_sees_it.portal = real_ns_client.portal

    refused = as_a_caller_sees_it.post("/v1/table/lance_catalog$db$judged/register", json={"location": "judged"})

    assert (refused.status_code, refused.json()["code"]) == (500, 18), refused.text
    assert real_ns_client.post("/v1/table/lance_catalog$db$judged/describe", json={}).status_code == 404


def test_describe_answers_the_file_format_the_table_was_written_at(real_ns_client: TestClient, tmp_path: Path) -> None:
    """A client writing fragments for `version/create` must match the table's file version and stable-row-id setting, and
    learns both from the detailed metadata, which the spec says requires loading the table ([[LH-260]]). Registered at
    2.1, a version the catalog never creates, so only the table's own manifest answers it (the register door refuses a
    table without stable row ids)."""
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    lance.write_dataset(pa.table({"id": [1]}), str(tmp_path / "legacy"), data_storage_version="2.1", enable_stable_row_ids=True)
    registered = real_ns_client.post("/v1/table/lance_catalog$db$legacy/register", json={"location": "legacy"})
    assert registered.status_code == 200, registered.text

    described = real_ns_client.post("/v1/table/lance_catalog$db$legacy/describe", json={"load_detailed_metadata": True})

    detailed = described.json()["metadata"]
    assert (described.status_code, detailed[DATA_STORAGE_VERSION_KEY], detailed[STABLE_ROW_IDS_KEY]) == (200, "2.1", "true"), described.text


#: A real body for the write doors, which refuse one that is not a valid Arrow IPC stream.
_ONE_ROW = encode_arrow_stream(pa.table({"id": [1]}))


def test_merge_insert_reads_its_query_params_as_the_spec_states_them(real_ns_client: TestClient) -> None:
    """A repeated ``on`` is one composite key, a ``*_filt`` is its clause's own condition, and ``when_not_matched_insert_all``
    defaults to false (``spec.yaml``).

    Main and a branch merge through one builder on the handle opened at the door's location ([[LH-530]]). A source row
    matches only a row equal on every key field ([[LH-537]]), so ``(9, 3)`` and ``(3, 9)`` match nothing although each
    field alone matches ``(3, 3)``, whose filter holds; the filter alone updates only the rows it holds true for, and a
    source row matching nothing is not inserted unless asked.
    """
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    target = encode_arrow_stream(
        pa.table({"id": pa.array([1, 2, 3], pa.int64()), "rev": pa.array([1, 2, 3], pa.int64()), "score": pa.array([0.9, 0.1, 0.9]), "label": ["a", "b", "c"]})
    )
    assert real_ns_client.post("/v1/table/lance_catalog$db$t/create", content=target, headers=ARROW_STREAM).status_code == 200
    source = encode_arrow_stream(
        pa.table(
            {
                "id": pa.array([1, 2, 9, 3], pa.int64()),
                "rev": pa.array([1, 2, 3, 9], pa.int64()),
                "score": pa.array([0.9, 0.1, 0.5, 0.5]),
                "label": ["A", "B", "C", "D"],
            }
        )
    )

    resp = real_ns_client.post(
        "/v1/table/lance_catalog$db$t/merge_insert?on=id&on=rev&when_matched_update_all_filt=target.score > 0.5&use_index=true",
        content=source,
        headers=ARROW_STREAM,
    )

    assert resp.status_code == 200, resp.text
    assert (resp.json()["num_updated_rows"], resp.json()["num_inserted_rows"]) == (1, 0)
    location = real_ns_client.post("/v1/table/lance_catalog$db$t/describe", json={}).json()["location"]
    assert sorted(lance.dataset(location).to_table(columns=["id", "rev", "label"]).to_pylist(), key=lambda row: (row["id"], row["rev"])) == [
        {"id": 1, "rev": 1, "label": "A"},
        {"id": 2, "rev": 2, "label": "b"},
        {"id": 3, "rev": 3, "label": "c"},
    ]


def test_list_table_versions_maps_descending_and_branch(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    fake_ns.list_table_versions.return_value = ListTableVersionsResponse(versions=[])
    # The door opens the named branch before the native call ([[LH-406]]); the table here has it.
    monkeypatch.setattr("catalog.services.table_claims.open_resolved_dataset_unchecked", lambda *a, **k: MagicMock())
    client.post("/v1/table/lance_catalog$db$t/version/list?descending=true&branch=exp")
    req = fake_ns.list_table_versions.call_args.args[0]
    assert req.descending is True
    assert req.branch == "exp"


# --- response shaping / content types (our logic) -------------------------- #


def test_exists_returns_200_with_no_body(client: TestClient, fake_ns: MagicMock) -> None:
    """Spec 0.9 conveys existence as a 200 whose response has **no `content:` block** —
    `lance_docs/ns_catalog/spec.yaml` gives it `description: Success, no content` and nothing else.
    A handler annotated `-> None` answers `application/json` with the four bytes `null`, which a
    strict client reads as a JSON document where the contract promised none. Both doors are pinned,
    because the two live in different modules and only one was ever measured."""
    fake_ns.table_exists.return_value = None
    fake_ns.namespace_exists.return_value = None
    for door in ("/v1/table/lance_catalog$db$t/exists", "/v1/namespace/lance_catalog$db/exists"):
        resp = client.post(door)
        assert resp.status_code == 200, door
        assert resp.content == b"", (door, resp.headers.get("content-type"), resp.content)


# --- error → HTTP / Problem-Details mapping (our logic) --------------------- #


def test_domain_conflict_maps_to_409(client: TestClient, fake_ns: MagicMock) -> None:
    # A declaration records its row through RegisterTable ([[LH-520]]), which answers a taken name so.
    fake_ns.register_table.side_effect = TableAlreadyExistsError("exists")
    assert client.post("/v1/table/lance_catalog$db$t/declare", json={}).status_code == 409


def test_backend_stub_message_maps_to_unsupported(client: TestClient, fake_ns: MagicMock) -> None:
    # native.call laundering: a backend that stubs an op with "not implemented" surfaces as 406 (spec
    # "unsupported"), not a 500. rename_table is now implemented in-process (#5b), so register_table — a
    # still-native-delegated op — stands in for a genuinely-unwired backend op.
    fake_ns.register_table.side_effect = RuntimeError("register_table not implemented")
    resp = client.post("/v1/table/lance_catalog$db$t/register", json={"location": "db-t"})
    assert (
        resp.status_code == 406
    )  # 406 since Q3 (2026-09-02): the spec's UnsupportedOperationErrorResponse is 406, and Lance's own reference server maps ErrorCode::Unsupported to NOT_ACCEPTABLE.
    assert resp.json()["status"] == 406


def test_list_branches_routes_to_dataset_branches(client: TestClient, monkeypatch) -> None:
    # Branches are now backed in-process via pylance `ds.branches` (was a native Unsupported).
    dataset = MagicMock()
    dataset.branches.list.return_value = {"exp": {"parent_branch": None, "parent_version": 2, "create_at": 1, "manifest_size": 9}}
    monkeypatch.setattr("catalog.services.table_claims.open_resolved_dataset_unchecked", lambda *a, **k: dataset)
    resp = client.post("/v1/table/lance_catalog$db1$users/branches/list")
    assert resp.status_code == 200
    assert resp.json()["branches"]["exp"]["parentVersion"] == 2  # serialized with the spec's camelCase alias


@pytest.fixture
def forked(real_ns_client: TestClient) -> tuple[TestClient, str]:
    """``db$t`` on real pylance: main at v3 holding 1, 2, 3, and ``exp`` forked from main's v2 and moved to its own v3 holding
    1, 2, 10, so a version named on ``exp`` and the same number on main hold different rows. Answers its location."""
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    table = "/v1/table/lance_catalog$db$t"

    def ok(path: str, **kwargs: Any) -> None:
        answered = real_ns_client.post(f"{table}/{path}", **kwargs)
        assert answered.status_code == 200, f"{path}: {answered.status_code} {answered.text}"

    ok("create", content=encode_arrow_stream(pa.table({"id": [1]})), headers=ARROW_STREAM)
    ok("insert", content=encode_arrow_stream(pa.table({"id": [2]})), headers=ARROW_STREAM)
    ok("branches/create", json={"name": "exp"})
    ok("insert?branch=exp", content=encode_arrow_stream(pa.table({"id": [10]})), headers=ARROW_STREAM)
    ok("insert", content=encode_arrow_stream(pa.table({"id": [3]})), headers=ARROW_STREAM)
    described = real_ns_client.post(f"{table}/describe", json={})
    return real_ns_client, described.json()["table_uri"]


@pytest.mark.parametrize(
    ("door", "body", "ref", "ids"),
    [
        pytest.param("branches/create", {"name": "m"}, ("m", None), [1, 2, 3], id="branch-from-latest-main"),
        pytest.param("branches/create", {"name": "v2", "from_version": 2}, ("v2", None), [1, 2], id="branch-from-a-main-version"),
        pytest.param("branches/create", {"name": "x", "from_branch": "exp", "from_version": 3}, ("x", None), [1, 2, 10], id="branch-from-a-branch-version"),
        pytest.param("tags/create", {"tag": "rc", "version": 3, "branch": "exp"}, "rc", [1, 2, 10], id="tag-on-a-branch-version"),
    ],
)
def test_a_ref_is_made_on_the_version_its_request_names(
    forked: tuple[TestClient, str], door: str, body: dict[str, object], ref: tuple[str, None] | str, ids: list[int]
) -> None:
    client, location = forked

    made = client.post(f"/v1/table/lance_catalog$db$t/{door}", json=body)

    assert made.status_code == 200, made.text
    assert sorted(lance.dataset(location).checkout_version(ref).to_table(columns=["id"]).column("id").to_pylist()) == ids


# --- #110 lineage-emit coverage: schema-evolution / index / restore / register / declare now emit, ---
# --- through the shared best-effort + version-pinned read-back trailer (lineage_deps).            ---


def _capture_emit(monkeypatch) -> dict[str, object]:
    """Record the kwargs of the versionless lifecycle marker a door sends, through the filing it sends from
    (`catalog.api.announcing`, [[LH-576]])."""
    captured: dict[str, object] = {}

    async def _cap(_emitter: object, _segments: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("catalog.api.announcing.emit_write_event", _cap)
    return captured


def _judge_as_single_version(monkeypatch) -> None:
    """The register door opens the dataset it attached, to mark its heads and to judge it; a MagicMock backend attaches none.

    Only the dataset reads are replaced — no heads to mark, and a single-version dataset with stable row ids declaring no
    base to judge — so the describe that resolves the location still runs. What the door does with a real mixed dataset is
    pinned in `services/catalog/tests/test_a_register_refuses_a_table_that_mixes_file_versions.py`, its location-exclusivity in
    `tests/integration/test_a_planted_base_is_refused_and_freezes_nothing.py`, and its heads in
    `tests/integration/test_a_commit_whose_announcement_was_lost_is_announced_once.py`.
    """
    from catalog.core.namespace import RegisteredDataset

    monkeypatch.setattr("catalog.services.commit_reconcile.mark_registered_heads", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "catalog.services.table_bases.registered_dataset_facts", lambda _location, _storage_options: RegisteredDataset(mixed=False, stable_row_ids=True)
    )


def _capture_measured_emit(monkeypatch) -> dict[str, object]:
    """Patch the shared trailer's emit_write_event to record what a measured op finally emits."""
    captured: dict[str, object] = {}

    async def _cap(_emitter: object, _segments: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("catalog.api.lineage_deps.emit_write_event", _cap)
    return captured


class _NativePanic(BaseException):
    """Where pylance's panic sits: ``pyo3_runtime.PanicException`` derives from BaseException alone and no module exports
    it (measured on pylance 14.0.3: its MRO is PanicException, BaseException, object), so a stand-in of that shape is the
    only way to raise one by choice."""


@pytest.mark.parametrize(
    ("where", "counted"),
    [
        pytest.param("catalog.services.tables.versions.open_resolved_dataset", None, id="the-pinned-open"),
        pytest.param("catalog.services.tables.versions.manifest_bytes.commit_time", None, id="the-commit-time"),
        pytest.param("catalog.services.tables.versions.facet_fields", None, id="the-schema-facet"),
        pytest.param("catalog.services.object_uuids.DescribeNamespaceRequest", None, id="the-namespace-uuid"),
        pytest.param("catalog.services.object_uuids.location_claims", None, id="the-table-uuid"),
        pytest.param("catalog.api.dependencies.namespace_for_warehouse", None, id="the-uuid-connection"),
        pytest.param("catalog.services.committed_files.read_transaction", "committed_files_unrecorded", id="the-landed-transaction"),
        pytest.param("catalog.services.committed_files.added", "committed_files_unrecorded", id="the-landed-files"),
    ],
)
def test_a_committed_write_whose_read_back_panics_answers_committed(
    real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, where: str, counted: str | None
) -> None:
    """The read-back runs after the write committed, so a native panic in it degrades the announcement and the door still
    answers the commit it made: a 500 for a write that landed sends the caller to retry it."""
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    assert (
        real_ns_client.post("/v1/table/lance_catalog$db$t/create", content=encode_arrow_stream(pa.table({"id": [1]})), headers=ARROW_STREAM).status_code == 200
    )

    def _panics(*_args: object, **_kwargs: object) -> object:
        raise _NativePanic("called `Result::unwrap()` on an `Err` value")

    # A module the read-back reaches through is replaced where it is looked up, so the door's own uses of it are untouched.
    monkeypatch.setattr(where, SimpleNamespace(read=_panics) if where.endswith("location_claims") else _panics)
    as_a_caller_sees_it = TestClient(real_ns_client.app, raise_server_exceptions=False)
    as_a_caller_sees_it.portal = real_ns_client.portal

    inserted = as_a_caller_sees_it.post("/v1/table/lance_catalog$db$t/insert", content=encode_arrow_stream(pa.table({"id": [2, 3]})), headers=ARROW_STREAM)

    location = real_ns_client.post("/v1/table/lance_catalog$db$t/describe", json={}).json()["location"]
    assert (inserted.status_code, inserted.json().get("version"), lance.dataset(location).count_rows()) == (200, 2, 3), inserted.text
    # What the read was for is counted as missed, never answered as the write's failure.
    assert counted is None or any(record.getMessage() == counted for record in caplog.records), [record.getMessage() for record in caplog.records]


def _panic_once_after(monkeypatch: pytest.MonkeyPatch, committed: str, read: str) -> None:
    """The first backend ``read`` after the backend ``committed`` panics; every other call is the backend's own."""
    from catalog.core import native

    real, seen = native.call, {"committed": False, "panicked": False}

    def call(ns: LanceNamespace, method_name: str, *args: object) -> object:
        if method_name == committed:
            seen["committed"] = True
        elif method_name == read and seen["committed"] and not seen["panicked"]:
            seen["panicked"] = True
            raise _NativePanic("called `Result::unwrap()` on an `Err` value")
        return real(ns, method_name, *args)

    monkeypatch.setattr(native, "call", call)


class _ListingPanics:
    """``refs`` as the branch door reaches it, whose branch listing panics."""

    def __getattr__(self, name: str) -> object:
        from catalog.services.tables import refs

        if name == "list_branches":
            raise _NativePanic("called `Result::unwrap()` on an `Err` value")
        return getattr(refs, name)


def _registered(client: TestClient, monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Any, str]:
    lance.write_dataset(pa.table({"id": [1]}), str(root / "registered"), data_storage_version="2.2", enable_stable_row_ids=True)
    _panic_once_after(monkeypatch, "register_table", "describe_table")
    return client.post("/v1/table/lance_catalog$db$registered/register", json={"location": "registered"}), "lance_catalog$db$registered"


def _declared(client: TestClient, monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Any, str]:
    _panic_once_after(monkeypatch, "register_table", "describe_table")
    return client.post("/v1/table/lance_catalog$db$declared/declare", json={}), "lance_catalog$db$declared"


def _branched(client: TestClient, monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Any, str]:
    assert client.post("/v1/table/lance_catalog$db$t/create", content=encode_arrow_stream(pa.table({"id": [1]})), headers=ARROW_STREAM).status_code == 200
    monkeypatch.setattr("catalog.api.v1.endpoints.branches.refs", _ListingPanics())
    return client.post("/v1/table/lance_catalog$db$t/branches/create", json={"name": "b"}), "lance_catalog$db$t"


def _undropped(client: TestClient, monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Any, str]:
    from catalog.core.config import get_settings

    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "1")
    get_settings.cache_clear()
    assert client.post("/v1/table/lance_catalog$db$t/create", content=encode_arrow_stream(pa.table({"id": [1]})), headers=ARROW_STREAM).status_code == 200
    assert client.post("/v1/table/lance_catalog$db$t/drop", json={}).status_code == 200
    monkeypatch.setattr("catalog.core.namespace.registered_dataset_facts", lambda *_a, **_k: _panics_now())
    return client.post("/management/v1/table/lance_catalog$db$t/undrop", json={}), "lance_catalog$db$t"


def _authored_on_a_branch(client: TestClient, monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Any, str]:
    from catalog.api.security import authenticate

    assert client.post("/v1/table/lance_catalog$db$t/create", content=encode_arrow_stream(pa.table({"id": [1]})), headers=ARROW_STREAM).status_code == 200
    assert client.post("/v1/table/lance_catalog$db$t/branches/create", json={"name": "b"}).status_code == 200
    caller = person(iss="https://idp.example", exp=2_000_000_000, iat=1_000_000_000)
    monkeypatch.setitem(app_of(client).dependency_overrides, authenticate, lambda: caller)
    # The author's record is keyed by the branch's incarnation, read off the branch listing after the append landed.
    monkeypatch.setattr("catalog.services.tables.versions.open_resolved_dataset_unchecked", lambda *_a, **_k: _panics_now())
    return client.post(
        "/v1/table/lance_catalog$db$t/insert?branch=b", content=encode_arrow_stream(pa.table({"id": [2]})), headers=ARROW_STREAM
    ), "lance_catalog$db$t"


class _InputResolvedOncePanics:
    """``table_claims`` as the event's uuids reach it: the input ``src`` resolves once, for the judge before the commit,
    and panics when the announcement resolves it again for its branch's incarnation."""

    def __init__(self) -> None:
        from catalog.services import table_claims

        self._real, self._seen = table_claims, 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)

    def resolve(self, ns: LanceNamespace, store: ClaimStore, segments: list[str]) -> object:
        if segments == ["lance_catalog", "db", "src"]:
            self._seen += 1
            if self._seen > 1:
                raise _NativePanic("called `Result::unwrap()` on an `Err` value")
        return self._real.resolve(ns, store, segments)


def _read_from_a_branch(client: TestClient, monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Any, str]:
    for table in ("src", "t"):
        assert (
            client.post(f"/v1/table/lance_catalog$db${table}/create", content=encode_arrow_stream(pa.table({"id": [1]})), headers=ARROW_STREAM).status_code
            == 200
        )
    assert client.post("/v1/table/lance_catalog$db$src/branches/create", json={"name": "feed"}).status_code == 200
    run = {
        "x-rask-run-id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
        "x-rask-job-namespace": "pipelines",
        "x-rask-job": "nightly",
        "x-rask-inputs": json.dumps([{"table": ["lance_catalog", "db", "src"], "version": 1, "branch": "feed"}]),
    }
    monkeypatch.setattr("catalog.api.dependencies.table_claims", _InputResolvedOncePanics())
    return client.post(
        "/v1/table/lance_catalog$db$t/insert", content=encode_arrow_stream(pa.table({"id": [2]})), headers={**ARROW_STREAM, **run}
    ), "lance_catalog$db$t"


def _created(client: TestClient, monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Any, str]:
    from catalog.services.tables import versions

    real, calls = versions.facet_fields, []

    def second_panics(schema: pa.Schema) -> object:
        # The read-back's facet comes first; the payload's, which a create announces, second.
        calls.append(schema)
        if len(calls) == 2:
            _panics_now()
        return real(schema)

    monkeypatch.setattr(versions, "facet_fields", second_panics)
    return client.post("/v1/table/lance_catalog$db$t/create", content=encode_arrow_stream(pa.table({"id": [1]})), headers=ARROW_STREAM), "lance_catalog$db$t"


def _panics_now() -> None:
    raise _NativePanic("called `Result::unwrap()` on an `Err` value")


@pytest.mark.parametrize(
    ("act", "counted"),
    [
        pytest.param(_created, None, id="create-the-payload-schema-facet"),
        pytest.param(_registered, None, id="register-the-described-location"),
        pytest.param(_declared, None, id="declare-the-described-location"),
        pytest.param(_branched, None, id="branch-create-the-recorded-parent"),
        pytest.param(_undropped, None, id="undrop-the-file-version-flags"),
        pytest.param(_authored_on_a_branch, "commit_author_unrecorded", id="insert-the-author-record-ref"),
        pytest.param(_read_from_a_branch, None, id="insert-the-input-branch-incarnation"),
    ],
)
def test_a_door_whose_read_after_its_commit_panics_answers_committed(
    real_ns_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    act: Callable[[TestClient, pytest.MonkeyPatch, Path], tuple[Any, str]],
    counted: str | None,
) -> None:
    """A door that reads the backend again after it committed degrades what that read adds when it panics, and answers
    the commit it made: a 500 for a change that landed sends the caller to make it again."""
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    as_a_caller_sees_it = TestClient(real_ns_client.app, raise_server_exceptions=False)
    as_a_caller_sees_it.portal = real_ns_client.portal

    answered, table = act(as_a_caller_sees_it, monkeypatch, tmp_path)

    described = real_ns_client.post(f"/v1/table/{table}/describe", json={})
    assert (answered.status_code, described.status_code) == (200, 200), answered.text
    # What the read was for is counted as missed, never answered as the change's failure.
    assert counted is None or any(record.getMessage() == counted for record in caplog.records), [record.getMessage() for record in caplog.records]


def test_create_exist_ok_reads_schema_back_instead_of_trusting_payload(real_ns_client: TestClient, monkeypatch) -> None:
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    # ExistOk KEEPS an existing table (nothing written, response.version = the existing version): the
    # payload's schema may then belong to a table that was never created — the true schema must be read back
    # PINNED at that version, never parsed from the payload. Now driven for real: the table genuinely exists.
    body = encode_arrow_stream(pa.table({"id": [1]}))
    assert real_ns_client.post("/v1/table/lance_catalog$db$t/create", content=body, headers=ARROW_STREAM).status_code == 200

    seen: dict[str, object] = {}

    def _readback(_ns: object, _so: object, _segments: object, pin_version: object = None, branch: object = None) -> object:
        seen["pin"] = pin_version
        return CommitReadback(version=cast(int, pin_version), schema_fields=[{"name": "true_col", "type": "int64"}], uri="s3://bucket/abc12345_db$t")

    def _payload_bomb(*_a: object, **_k: object) -> object:
        raise AssertionError("ExistOk create must not trust the request payload's schema")

    # Patch the enrichment functions only AFTER the setup create, so the bomb guards the exist_ok call alone.
    monkeypatch.setattr("catalog.services.tables.versions.read_version_and_schema", _readback)
    monkeypatch.setattr("catalog.services.tables.versions.payload_schema_fields", _payload_bomb)

    resp = real_ns_client.post("/v1/table/lance_catalog$db$t/create?mode=exist_ok", content=body, headers=ARROW_STREAM)
    assert resp.status_code == 200
    assert "pin" in seen  # the readback path was taken (not the payload path)
    assert seen["pin"] == 1  # schema read back pinned at the EXISTING (freshly-created) table's version


def test_deregister_emits_marker_before_revoking_tuples(client: TestClient, fake_ns: MagicMock, monkeypatch, claim_described: Callable[[str], str]) -> None:
    # The DEREGISTER_TABLE marker must publish BEFORE revoke_ownership: a revoke that fails ends the request, and a
    # marker that waited for it would leave the feed showing the table as live.
    from lance_namespace import DeregisterTableResponse

    fake_ns.deregister_table.return_value = DeregisterTableResponse()
    order: list[str] = []

    async def _emit(*_a: object, **_k: object) -> None:
        order.append("emit")

    async def _revoke(*_a: object, **_k: object) -> None:
        order.append("revoke")

    monkeypatch.setattr("catalog.api.announcing.emit_write_event", _emit)
    monkeypatch.setattr("catalog.api.authz.object_grants.revoke_ownership", _revoke)
    claim_described("lance_catalog$db$t")

    resp = client.post("/v1/table/lance_catalog$db$t/deregister")
    assert resp.status_code == 200
    assert order == ["emit", "revoke"]  # marker first, while the caller's grant still authorizes ingest


def test_register_emits_the_resolved_location_not_the_relative_one(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # The regression this closes, at the layer that would have caught it: `register_table` ECHOES the
    # caller's path, so a caller registering `t.lance` had `t.lance` stamped as the marker's `source_uri`
    # — which opens as nothing, classifies MISSING_ON_STORAGE, and reports a live registered table as
    # storage loss forever. `describe_table` resolves it against the table's own root.
    from lance_namespace import DescribeTableResponse, RegisterTableResponse

    fake_ns.register_table.return_value = RegisterTableResponse(location="t.lance")
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://bucket/9f_db$t")
    _judge_as_single_version(monkeypatch)
    captured = _capture_emit(monkeypatch)

    resp = client.post("/v1/table/lance_catalog$db$t/register", json={"location": "t.lance"})
    assert resp.status_code == 200
    assert captured["source_uri"] == "s3://bucket/9f_db$t", "the marker must carry a URI the sweep can open"


def test_declare_emits_versionless_marker(client: TestClient, fake_ns: MagicMock, monkeypatch, claim_described: Callable[[str], str]) -> None:
    # The declaration answers the location the backend describes once its row is recorded ([[LH-520]]), claimed
    # under the uuid the declaration's grants are seeded on.
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://bucket/t")
    claim_described("lance_catalog$db$t")
    captured = _capture_emit(monkeypatch)

    resp = client.post("/v1/table/lance_catalog$db$t/declare", json={})
    assert resp.status_code == 200
    assert captured["operation"] == "declare_table"
    assert captured["version"] is None  # reserved, no data yet
    assert captured["source_uri"] == "s3://bucket/t"


# --- version describe: the native binding is `request: dict`-typed; native.call must marshal the pydantic ---
# --- request to a dict, else a TypeError surfaces as a fake 501. These guard that fix (audit finding). ---


def test_describe_version_marshals_request_to_a_dict(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table_version.return_value = DescribeTableVersionResponse(version=TableVersion(version=2, manifest_path="_versions/2.manifest"))
    resp = client.post("/v1/table/lance_catalog$db$t/version/describe?version=2")
    assert resp.status_code == 200
    assert resp.json()["version"]["version"] == 2
    arg = fake_ns.describe_table_version.call_args.args[0]
    assert isinstance(arg, dict) and arg["id"] == ["lance_catalog", "db", "t"] and arg["version"] == 2  # a dict, not the model


def test_describe_version_missing_maps_to_404(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table_version.side_effect = TableVersionNotFoundError("version 99 not found")
    resp = client.post("/v1/table/lance_catalog$db$t/version/describe?version=99")
    assert resp.status_code == 404


def test_request_validation_on_a_spec_route_maps_to_400_problem_json(client: TestClient) -> None:
    """`UpdateTable` is a spec operation, so its errors answer the spec's contract.

    This asserted 422 until 2026-09-14. The vendored spec contains ZERO 422s and maps `InvalidInput`
    to 400 — which `ns_errors` itself already encodes in both directions (`_STATUS`,
    `_STATUS_CODE_FALLBACK`) — so serving that same code at 422 made one server answer one condition
    two ways, and a generated Lance client dispatches on the CODE.
    """
    # UpdateTableRequest requires `updates`; an empty body fails validation.
    resp = client.post("/v1/table/lance_catalog$db$t/update", json={})
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["title"] == "Validation Error"


# --- data-plane request building (our logic; fake dataset) ----------------- #


def test_alter_columns_converts_json_arrow_type_to_pyarrow(real_ns_client: TestClient) -> None:
    # A re-type carries `data_type` in the spec's JSON form (`JsonArrowDataType`), the form DescribeTable answers a
    # column's type in; it must reach pylance as a real pa.DataType. float32→float16 is the documented vector case
    # (`lance_docs/lance/guide/data_evolution.md` § Casting column data types); utf8→large_utf8 and
    # list<int32>→large_list<int32> are casts pylance 14.0.3 performs (measured), each spelled as DescribeTable itself
    # spells the type of a column that already holds it. A name is read as the namespace's own parser reads it, case
    # aside and `boolean` for `bool`; `double` is a spelling that parser refuses, and so does the door (400, 13).
    _ensure_namespace(real_ns_client, "lance_catalog$db")
    rows = pa.table(
        {
            "embedding": pa.array([0.5, 1.5], pa.float32()),
            "text": pa.array(["a", "b"], pa.string()),
            "large_text": pa.array(["a", "b"], pa.large_string()),
            "items": pa.array([[1], [2]], pa.list_(pa.int32())),
            "large_items": pa.array([[1], [2]], pa.large_list(pa.int32())),
            "count": pa.array([1, 2], pa.int32()),
            "flag": pa.array([True, False]),
        }
    )
    assert real_ns_client.post("/v1/table/lance_catalog$db$t/create", content=encode_arrow_stream(rows), headers=ARROW_STREAM).status_code == 200
    described = real_ns_client.post("/v1/table/lance_catalog$db$t/describe", json={"load_detailed_metadata": True}).json()
    spelled = {field["name"]: field["type"] for field in described["schema"]["fields"]}
    alterations = [
        {"path": "embedding", "data_type": {"type": "float16"}},
        {"path": "text", "data_type": spelled["large_text"]},
        {"path": "items", "data_type": spelled["large_items"]},
        {"path": "count", "data_type": {"type": "INT64"}},
        {"path": "flag", "data_type": {"type": "boolean"}},
    ]
    refused = real_ns_client.post("/v1/table/lance_catalog$db$t/alter_columns", json={"alterations": [{"path": "embedding", "data_type": {"type": "double"}}]})
    assert (refused.status_code, refused.json()["code"]) == (400, 13), refused.text
    resp = real_ns_client.post("/v1/table/lance_catalog$db$t/alter_columns", json={"alterations": alterations})
    assert resp.status_code == 200, resp.text
    schema = lance.dataset(described["location"]).schema
    assert [schema.field(name).type for name in ("embedding", "text", "items", "count", "flag")] == [
        pa.float16(),
        pa.large_string(),
        pa.large_list(pa.int32()),
        pa.int64(),
        pa.bool_(),
    ]


def test_alter_columns_unsupported_type_is_400_not_500(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("catalog.services.table_claims.open_resolved_dataset", lambda *a, **k: MagicMock())
    resp = client.post(
        "/v1/table/lance_catalog$db$t/alter_columns",
        json={"alterations": [{"path": "c", "data_type": {"type": "some_exotic_type"}}]},
    )
    assert resp.status_code == 400  # clear InvalidInput, not a silent Rust-boundary 500


def test_update_with_empty_updates_is_400(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("catalog.services.table_claims.open_resolved_dataset", lambda *a, **k: MagicMock())
    resp = client.post("/v1/table/lance_catalog$db$t/update", json={"updates": []})
    assert resp.status_code == 400
    assert resp.json()["code"] == 13  # InvalidInput


def test_maintenance_policy_crud_round_trips(client: TestClient, fake_ns: MagicMock, claim_described: Callable[[str], str]) -> None:
    # CONTRACT (#50, ADR 0173 §8): set keys the record by the uuid the table's location claim records and persists it;
    # describe returns the record; delete is idempotent, and describe then answers that none is set ([[LH-589]]).
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://lance-test-root/u1_db1$users")
    users = claim_described("lance_catalog$db1$users")
    set_resp = client.post("/management/v1/table/lance_catalog$db1$users/policy/set", json={"retention_days": 7, "compact_interval_hours": 12})
    assert set_resp.status_code == 200, set_resp.text
    body = set_resp.json()
    assert body["uuid"] == users and body["retention_days"] == 7

    desc = client.post("/management/v1/table/lance_catalog$db1$users/policy/describe")
    assert (desc.status_code, desc.json()["compact_interval_hours"], desc.json()["set"]) == (200, 12, True)

    assert client.post("/management/v1/table/lance_catalog$db1$users/policy/delete").status_code == 200
    unset = client.post("/management/v1/table/lance_catalog$db1$users/policy/describe")
    assert (unset.status_code, unset.json()) == (200, {"kind": "table", "id": "lance_catalog$db1$users", "uuid": users, "set": False})


def test_a_policy_record_that_cannot_be_read_is_a_fault_until_its_owner_deletes_it(
    client: TestClient, fake_ns: MagicMock, claim_described: Callable[[str], str]
) -> None:
    """A record stored for the table that is no policy may be a hold: describe never answers that none is set, and a set
    never merges onto nothing over it, which would drop the hold. Its owner deletes it and sets the policy they mean."""
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://lance-test-root/u1_db1$users")
    from catalog.core.config import get_settings
    from lakehouse.records import maintenance_policies, record_store
    from lakehouse.records.record_store import record_key

    users = claim_described("lance_catalog$db1$users")
    settings = get_settings()
    held = {"kind": "table", "id": ["not", "text"], "uuid": users, "cleanup_enabled": False}
    seeding.put_policy(settings.registry_root, settings.storage_options(), held)
    door = "/management/v1/table/lance_catalog$db1$users/policy"

    described = client.post(f"{door}/describe")
    refused = client.post(f"{door}/set", json={"retention_days": 7})
    stored = record_store.read_record(settings.registry_root, settings.storage_options(), record_key(maintenance_policies.PREFIX, "table", users))
    deleted = client.post(f"{door}/delete")
    replaced = client.post(f"{door}/set", json={"retention_days": 7})

    faults = [(answer.status_code, answer.headers["content-type"], answer.json()["code"]) for answer in (described, refused)]
    assert faults == [(500, "application/problem+json", 18)] * 2
    assert stored == held, "the refused set changed the record it could not read"
    assert (deleted.status_code, replaced.status_code, replaced.json()["retention_days"]) == (200, 200, 7)


@pytest.mark.parametrize(
    ("meanwhile", "kept"),
    [
        pytest.param("set", {"compact_enabled": False, "scan_batch_size": 64}, id="a-concurrent-set-keeps-its-field"),
        pytest.param("delete", {"compact_enabled": False, "scan_batch_size": None}, id="a-concurrent-delete-is-not-undone"),
    ],
)
def test_a_policy_set_merges_onto_the_record_it_replaces(
    client: TestClient, fake_ns: MagicMock, monkeypatch: pytest.MonkeyPatch, meanwhile: str, kept: dict[str, object]
) -> None:
    """A partial set merges onto the record as it stands when its write lands, never one it read before: a set landing
    meanwhile keeps the field it set, and a delete landing meanwhile is not undone by the fields the set read before it."""
    from catalog.api.v1.endpoints import policies as policy_doors
    from catalog.core.config import get_settings
    from lakehouse.records import maintenance_policies

    assert client.post("/management/v1/namespace/lance_catalog$served/policy/set", json={"retain_versions": 5}).status_code == 200
    real_record = policy_doors._record  # noqa: SLF001 — the merge, where another request's write is landed in between
    landed: list[str] = []

    def _another_request_lands(
        kind: policy_doors.PolicyKind, canonical: str, body: Any, existing: maintenance_policies.PolicyRecord | None, *, uuid: str | None
    ) -> maintenance_policies.PolicyRecord:
        if not landed:
            landed.append(meanwhile)
            settings = get_settings()
            if meanwhile == "set":
                record = real_record(kind, canonical, policy_doors.PolicyRequest(scan_batch_size=64), existing=existing, uuid=uuid)
                seeding.put_policy(settings.registry_root, settings.storage_options(), record.stored())
            else:
                maintenance_policies.delete_policy(settings.registry_root, settings.storage_options(), kind, key=str(uuid))
        return real_record(kind, canonical, body, existing=existing, uuid=uuid)

    monkeypatch.setattr(policy_doors, "_record", _another_request_lands)

    answer = client.post("/management/v1/namespace/lance_catalog$served/policy/set", json={"compact_enabled": False})

    described = client.post("/management/v1/namespace/lance_catalog$served/policy/describe").json()
    assert answer.status_code == 200, answer.text
    assert {field: described.get(field) for field in kept} == kept, described
    assert described.get("retain_versions") == (5 if meanwhile == "set" else None), described


def test_namespace_policy_describe_answers_the_policy_its_set_door_wrote(client: TestClient, fake_ns: MagicMock) -> None:
    # [[LH-589]]: the namespace door reads the record its set door keyed by the namespace's uuid, and says it is set.
    uuid = fake_ns.describe_namespace.return_value.properties[UUID_PROPERTY]
    assert client.post("/management/v1/namespace/lance_catalog$served/policy/set", json={"retain_versions": 5}).status_code == 200

    described = client.post("/management/v1/namespace/lance_catalog$served/policy/describe")

    # The defaults a first set writes are kept on the record, so they are part of what it reads back.
    kept = {"retain_versions": 5, "compact_enabled": True, "cleanup_enabled": True, "optimize_indices_enabled": True}
    assert (described.status_code, described.json()) == (200, {"kind": "namespace", "id": "lance_catalog$served", "uuid": uuid, **kept, "set": True})


def test_a_field_a_rask_door_does_not_define_is_refused_and_nothing_is_written(client: TestClient, fake_ns: MagicMock) -> None:
    # [[LH-576]] E10e: every body of rask's own doors refuses a field it does not define (`schemas.RequestBody`), so a
    # misspelled bound is refused by name rather than read as absent and answered as if it had been understood.
    del fake_ns
    refused = client.post("/management/v1/namespace/lance_catalog$served/policy/set", json={"retain_versions": 5, "retention_day": 7})

    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == 13
    assert [(error["field"], error["type"]) for error in refused.json()["errors"]] == [("body.retention_day", "extra_forbidden")]
    described = client.post("/management/v1/namespace/lance_catalog$served/policy/describe")
    assert (described.status_code, described.json()["set"]) == (200, False), "a refused set wrote a policy"


def _project_policy_settings(tmp_path: object):

    # A LOCAL control root so the warehouse registry + policy records round-trip on the FS per test
    # (the shared fixture's fixed /tmp root would leak records across tests).
    return catalog_settings(impl="dir", root="s3://lance-catalog", control_root=f"file://{tmp_path}")


def test_project_policy_crud_round_trips(client: TestClient, tmp_path: object) -> None:
    # CONTRACT (#84): set resolves the project's ACTIVE warehouse buckets from the registry at set time
    # and persists them on the record; describe returns it; delete is idempotent, then describe answers that none is set.
    from catalog.api.dependencies import request_settings

    s = _project_policy_settings(tmp_path)
    app_of(client).dependency_overrides[request_settings] = lambda: s
    so = s.storage_options()
    seeding.put_project(s.registry_root, so, {"id": "acme", "created_at": "t", "created_by": "seed"})
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-b", "bucket": "acme-old", "project": "acme", "status": "deactivated"})
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-c", "bucket": "other-wh", "project": "other", "status": "active"})

    set_resp = client.post("/management/v1/project/acme/policy/set", json={"retention_days": 90})
    assert set_resp.status_code == 200, set_resp.text
    body = set_resp.json()
    # Only the project's own ACTIVE bucket is covered — never a deactivated one or another tenant's.
    assert body["kind"] == "project" and body["buckets"] == ["acme-wh"] and body["retention_days"] == 90

    desc = client.post("/management/v1/project/acme/policy/describe")
    assert (desc.status_code, desc.json()["buckets"], desc.json()["set"]) == (200, ["acme-wh"], True)

    assert client.post("/management/v1/project/acme/policy/delete").status_code == 200
    unset = client.post("/management/v1/project/acme/policy/describe")
    assert (unset.status_code, unset.json()) == (200, {"kind": "project", "id": "acme", "set": False})


def test_project_policy_describe_of_a_project_with_no_record_is_not_found(client: TestClient, tmp_path: object) -> None:
    # [[LH-589]]: a project exists when its registry record does, so a policy read of one without a record answers
    # the missing project's 404 code 4, never that it holds no policy.
    from catalog.api.dependencies import request_settings

    app_of(client).dependency_overrides[request_settings] = lambda: _project_policy_settings(tmp_path)

    resp = client.post("/management/v1/project/ghost/policy/describe")

    assert (resp.status_code, resp.json()["code"]) == (404, 4)


def test_project_policy_set_refused_without_an_active_warehouse(client: TestClient, tmp_path: object) -> None:
    # A policy that could never match anything must fail loudly at set time, not lie dormant.
    from catalog.api.dependencies import request_settings

    app_of(client).dependency_overrides[request_settings] = lambda: _project_policy_settings(tmp_path)
    resp = client.post("/management/v1/project/ghost/policy/set", json={"retention_days": 90})
    assert resp.status_code == 400
    assert "no active warehouse" in resp.json()["error"]


def test_project_policy_set_refused_on_a_cross_claimed_bucket(client: TestClient, tmp_path: object) -> None:
    # Mallory layer 2 (audit 2026-07-23, defense in depth): even if a rival bucket claim got PAST the
    # create_warehouse guards (records written straight into the registry here), a policy set that would
    # resolve a bucket another project's warehouse also claims must be refused — never a retention policy
    # over the other tenant's data.
    from catalog.api.dependencies import request_settings

    s = _project_policy_settings(tmp_path)
    app_of(client).dependency_overrides[request_settings] = lambda: s
    so = s.storage_options()
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "shared-bkt", "project": "acme", "status": "active"})
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-evil", "bucket": "shared-bkt", "project": "evil", "status": "active"})
    resp = client.post("/management/v1/project/evil/policy/set", json={"retention_days": 1, "retain_versions": 1})
    assert resp.status_code == 409, resp.text


def test_project_policy_set_is_refused_while_a_registry_record_cannot_be_read(client: TestClient, tmp_path: object) -> None:
    # A warehouse record that cannot be read may be one of the project's own, whose bucket the policy would leave on the
    # defaults, or another project's claim on a bucket it would govern ([[LH-572]]): the set refuses and writes nothing.
    from pathlib import Path

    from catalog.api.dependencies import request_settings

    s = _project_policy_settings(tmp_path)
    app_of(client).dependency_overrides[request_settings] = lambda: s
    seeding.put_warehouse(
        s.registry_root,
        s.storage_options(),
        {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"},
    )
    (Path(str(tmp_path)) / "_warehouses" / "zzz-corrupt.json").write_text("{truncated")

    resp = client.post("/management/v1/project/acme/policy/set", json={"retention_days": 90})

    assert (resp.status_code, resp.headers["content-type"], resp.json().get("code")) == (500, "application/problem+json", 18), resp.text
    assert client.post("/management/v1/project/acme/policy/describe").status_code == 404


def test_project_policy_rejects_a_malformed_project_id(client: TestClient, tmp_path: object) -> None:
    from catalog.api.dependencies import request_settings

    app_of(client).dependency_overrides[request_settings] = lambda: _project_policy_settings(tmp_path)
    resp = client.post("/management/v1/project/Bad_Name/policy/set", json={"retention_days": 90})
    assert resp.status_code == 400


# --- GET /management/v1/projects/{id}/policies — the #65 project-scoped VIEW -------------------------- #
#
# Real stores throughout (a local-FS control root through pyarrow.fs): real warehouse records and real policy records
# written by the same `put_policy` the catalog uses. The one thing these must prove is that scoping is derived from the
# warehouse a name begins with ([[LH-593]]) and not from a bucket, because `set_namespace_policy` stores the DEFAULT
# root on every record (see the policies.py module docstring).


def _list_policies_for(client: TestClient, project: str) -> dict[str, Any]:
    """The listing, asserted 200 — the settings override is already in place at every call site."""
    resp = client.get(f"/management/v1/projects/{project}/policies")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict)
    return body


def test_project_policies_list_scopes_to_the_projects_own_buckets(client: TestClient, tmp_path: object) -> None:
    # CONTRACT (#65): a tenant's policy view carries the tenant's records and NOTHING else, each table's under the
    # name the table holds now ([[LH-523]]). Two warehouses, a table policy in each — the rival's record must never
    # cross, and acme's table, renamed since its policy was set, is listed as `wh-a$db$u`.
    from catalog.api.dependencies import request_settings
    from catalog.services import table_claims
    from lakehouse.records import location_claims

    s = _project_policy_settings(tmp_path)
    app_of(client).dependency_overrides[request_settings] = lambda: s
    so = s.storage_options()
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-z", "bucket": "other-wh", "project": "other", "status": "active"})
    renamed, location = str(uuid4()), f"s3://acme-wh/{uuid4()}"
    # The rename handed the table's claim, and the uuid it records, to the table's new name.
    location_claims.take(table_claims.claim_store(s), location, "wh-a$db$u", segments=["wh-a", "db", "u"], is_gone=lambda _claim: False, uuid=renamed)
    seeding.put_policy(s.registry_root, so, {"kind": "table", "id": "wh-a$db$t", "uuid": renamed, "location": location, "retention_days": 7})
    seeding.put_policy(s.registry_root, so, {"kind": "table", "id": "wh-z$rival$t", "uuid": str(uuid4()), "retention_days": 1})

    body = _list_policies_for(client, "acme")
    assert [p["id"] for p in body["policies"]] == ["wh-a$db$u"]
    assert (body["buckets"], body["warehouses"]) == (["acme-wh"], ["wh-a"])


def test_project_policies_list_finds_a_namespace_policy_by_its_warehouse(client: TestClient, tmp_path: object) -> None:
    # THE decisive one: a namespace's record names no bucket, only the namespace, so the listing finds it
    # through the warehouse its name begins with.
    from catalog.api.dependencies import request_settings

    s = _project_policy_settings(tmp_path)
    app_of(client).dependency_overrides[request_settings] = lambda: s
    so = s.storage_options()
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    # What set_namespace_policy writes: keyed by the namespace's uuid, with the name it had kept under `id`.
    seeding.put_policy(s.registry_root, so, {"kind": "namespace", "id": "wh-a$served", "uuid": str(uuid4()), "retention_days": 30})

    body = _list_policies_for(client, "acme")
    assert [p["id"] for p in body["policies"]] == ["wh-a$served"]
    assert body["warehouses"] == ["wh-a"]  # the scope the listing was computed from, reported


def test_project_policies_list_includes_a_deactivated_warehouses_bucket(client: TestClient, tmp_path: object) -> None:
    # Reading coverage is not writing it: `set_project_policy` filters to ACTIVE warehouses because a
    # policy over a dead bucket could never match, but a deactivated warehouse's data — and any policy
    # still governing it — is still the tenant's. Narrowing here would hide the retention record an
    # operator is looking for at exactly the moment they are offboarding.
    from catalog.api.dependencies import request_settings
    from catalog.services import table_claims
    from lakehouse.records import location_claims

    s = _project_policy_settings(tmp_path)
    app_of(client).dependency_overrides[request_settings] = lambda: s
    so = s.storage_options()
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-old", "bucket": "acme-old", "project": "acme", "status": "deactivated"})
    table_uuid, location = str(uuid4()), f"s3://acme-old/{uuid4()}"
    location_claims.take(
        table_claims.claim_store(s), location, "wh-old$arch$t", segments=["wh-old", "arch", "t"], is_gone=lambda _claim: False, uuid=table_uuid
    )
    seeding.put_policy(s.registry_root, so, {"kind": "table", "id": "wh-old$arch$t", "uuid": table_uuid, "location": location, "compact_enabled": False})

    body = _list_policies_for(client, "acme")
    assert [p["id"] for p in body["policies"]] == ["wh-old$arch$t"]
    assert body["buckets"] == ["acme-old"]


def test_project_policies_list_returns_only_its_own_project_record(client: TestClient, tmp_path: object) -> None:
    # A project record matches by ID and by nothing else. The rival record here claims acme's own bucket —
    # the contested-registry state `set_project_policy` refuses at write time — so a bucket match would
    # surface another tenant's project policy inside acme's view.
    from catalog.api.dependencies import request_settings

    s = _project_policy_settings(tmp_path)
    app_of(client).dependency_overrides[request_settings] = lambda: s
    so = s.storage_options()
    seeding.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    seeding.put_policy(s.registry_root, so, {"kind": "project", "id": "acme", "buckets": ["acme-wh"], "retention_days": 90})
    seeding.put_policy(s.registry_root, so, {"kind": "project", "id": "other", "buckets": ["acme-wh"], "retention_days": 1})

    body = _list_policies_for(client, "acme")
    assert [(p["kind"], p["id"]) for p in body["policies"]] == [("project", "acme")]


# The listing's own 403 lives with the other three project-policy gates, in `test_authz.py`
# (`test_project_policies_list_requires_project_admin`) — one file owns the authz contract, and a
# fourth door asserted somewhere else is how a tier splits without anyone noticing.


def test_access_list_is_unsupported_without_fga(client: TestClient) -> None:
    # CONTRACT (#51): an auth-off stack has no grants to review — answer Unsupported honestly instead
    # of an empty grant list that would read as "nobody has access".
    resp = client.post("/management/v1/table/lance_catalog$db1$users/access/list")
    # 406 since Q3 (2026-09-02) — the spec's status for Unsupported. The detail now survives too: it is
    # a 4xx, so the 5xx redaction that used to mask it no longer applies, and the caller is told which
    # capability is off rather than only that something is.
    assert resp.status_code == 406


def test_maintenance_policy_rejects_an_empty_policy(client: TestClient, fake_ns: MagicMock, claim_described: Callable[[str], str]) -> None:
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://lance-test-root/u1_db1$users")
    users = claim_described("lance_catalog$db1$users")
    resp = client.post("/management/v1/table/lance_catalog$db1$users/policy/set", json={})
    assert resp.status_code == 422  # a body that sets nothing changes nothing — the validator refuses
    unset = client.post("/management/v1/table/lance_catalog$db1$users/policy/describe")
    assert (unset.status_code, unset.json()) == (200, {"kind": "table", "id": "lance_catalog$db1$users", "uuid": users, "set": False}), (
        "a refused set wrote a policy"
    )
    # But an EXPLICIT compact_enabled=true is meaningful (a table-level re-enable under a disabled
    # namespace policy — the table's own record shadows the namespace record), so it must be accepted.
    resp = client.post("/management/v1/table/lance_catalog$db1$users/policy/set", json={"compact_enabled": True})
    assert resp.status_code == 200, resp.text
    # The client fixture's registry root is a fixed local path — delete so no record leaks across tests.
    assert client.post("/management/v1/table/lance_catalog$db1$users/policy/delete").status_code == 200
