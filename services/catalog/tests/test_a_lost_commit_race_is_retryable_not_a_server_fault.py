"""A door's commit racing another commit lands, planned again on the version that won; none answers a server fault.

Every column op commits stamped ([[LH-497]], `dataplane.commit_stamped`): Lance's own operation is planned on the
latest version and finalized as exactly the next one through the backend's put-if-not-exists, and a writer that lost
that version plans again on the new latest. So six concurrent `add_columns` on one table, each adding its own column,
all land, in six consecutive versions. A writer losing every one of its attempts answers 14 `ConcurrentModification`
(409), the spec's code for a retryable race. A commit that lost its race takes its ref's turn ([[LH-259]],
`lakehouse.commit_turns`), so it lands even beside a writer appending in a tight loop.

THE RACES ARE REAL, NOT SIMULATED. Threads commit against one dataset on a real `dir` root. Neither test is marked slow:
measured at a few seconds, because nothing large is written.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import (
    AlterTableDropColumnsRequest,
    ConcurrentModificationError,
    CreateNamespaceRequest,
    CreateTableVersionRequest,
    DeleteFromTableRequest,
    InsertIntoTableRequest,
    RestoreTableRequest,
    ServiceUnavailableError,
    UpdateTableRequest,
    connect,
)

from catalog.services import table_claims
from catalog.services.dataplane import (
    add_columns,
    committed_manifest_digest,
    create_table,
    delete_from_table,
    drop_columns,
    insert_into_table,
    restore,
    update_table,
)
from catalog.services.table_claims import DoorTable
from lakehouse import base_registry, commit_records, file_identities
from lakehouse.location_claims import ClaimStore
from lakehouse.storage_options import StorageOptions


lance = pytest.importorskip("lance")

from lance_namespace import AlterTableAddColumnsRequest  # noqa: E402
from lance_namespace_urllib3_client.models.add_columns_entry import AddColumnsEntry  # noqa: E402


TABLE_ID = ["db", "rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64())])

#: Writers racing for the same next version: one wins each version, the rest plan again.
WRITERS = 6

#: The stamp a door commits with ([[LH-497]]).
STAMP = {"rask.author": "alice", "rask.operation": "add_columns"}


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    namespace.create_namespace(CreateNamespaceRequest(id=TABLE_ID[:-1]))
    create_table(
        namespace,
        StorageOptions(),
        TABLE_ID,
        pa.table({"id": pa.array([1, 2, 3], pa.int64())}, schema=SCHEMA),
        root=str(tmp_path / "data"),
        mode="create",
        registry=base_registry.BaseRegistry(control_root=str(tmp_path / "control"), storage_options={}),
        holder="$".join(TABLE_ID),
    )
    return namespace


@pytest.fixture
def table(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """The table as a door acts on it, resolved on ``ns``."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID, announces=lambda _operation: True)


def test_concurrent_add_columns_each_land_once_and_a_lost_attempt_leaves_nothing(table: DoorTable, tmp_path: Path) -> None:
    """Each landed version keeps the author record its commit made before its finalize, and an attempt that lost its race
    deletes its own (ADR 0187), with every file it wrote and their committed-files record ([[LH-564]]): the table holds
    only the transaction and data files its versions name."""

    def add(i: int) -> int | BaseException:
        request = AlterTableAddColumnsRequest(id=TABLE_ID, new_columns=[AddColumnsEntry(name=f"c{i}", expression="id + 1")])
        try:
            return add_columns(table, StorageOptions(), request, transaction_properties=STAMP).version
        except BaseException as exc:  # noqa: BLE001 — every writer's outcome is inspected
            return exc

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        outcomes = list(pool.map(add, range(WRITERS)))

    failed = [o for o in outcomes if isinstance(o, BaseException)]
    assert all(isinstance(o, ConcurrentModificationError) and o.code == 14 for o in failed), f"a racing add_columns answered {failed!r}"
    landed = sorted(o for o in outcomes if isinstance(o, int))
    assert landed == list(range(2, 2 + len(landed))), landed
    assert len(landed) == WRITERS, f"{len(failed)} of {WRITERS} writers lost all their attempts: {failed!r}"
    names = set(table.open(StorageOptions()).schema.names)
    assert {f"c{i}" for i in range(WRITERS)} <= names, names
    table_uuid = table.resolved.require_uuid()
    signed = [_signed(table, str(tmp_path / "control"), table_uuid, version) for version in landed]
    kept = list((tmp_path / "control" / commit_records.AUTHORS_PREFIX / table_uuid).rglob("*.json"))
    assert (signed, len(kept)) == ([("alice", True)] * WRITERS, WRITERS)
    latest = table.open(StorageOptions())
    root = Path(table.location.removeprefix("file://"))
    committed = {t.uuid for t in (latest.read_transaction(v["version"]) for v in latest.versions()) if t is not None}
    transactions = {path.name.split("-", 1)[1].removesuffix(".txn") for path in (root / "_transactions").iterdir()}
    named = {data_file.path for fragment in latest.get_fragments() for data_file in fragment.data_files()}
    data = {path.name for path in (root / "data").iterdir()}
    records = {r.transaction_uuid for r in file_identities.recorded(str(tmp_path / "control"), StorageOptions(), table_uuid)}
    staged = [path.name for path in (root / "_versions").iterdir() if ".manifest-" in path.name]
    assert (transactions, data, records - committed, staged) == (committed, named, set(), [])


def test_a_restore_whose_attempt_loses_deletes_only_its_transaction_file(table: DoorTable, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost attempt deletes what it wrote ([[LH-564]]), and a Restore writes no data: the files it names as new over the
    version it follows are the restored version's, which that version still holds. So its lost attempt deletes only its
    own transaction file, and the attempt after it restores the rows."""
    update_table(table, StorageOptions(), UpdateTableRequest(id=TABLE_ID, predicate="id >= 0", updates=[["id", "id + 10"]]), transaction_properties=STAMP)
    finalize = table_claims.DoorTable.finalize_version
    lost: list[str] = []

    def loses_once(door: DoorTable, request: CreateTableVersionRequest, *, turn: str | None = None) -> object:
        if not lost:
            lost.append(request.manifest_path)
            raise ConcurrentModificationError("another commit took the version first")
        return finalize(door, request, turn=turn)

    monkeypatch.setattr(table_claims.DoorTable, "finalize_version", loses_once)

    _answer, version = restore(table, StorageOptions(), RestoreTableRequest(id=TABLE_ID, version=1), transaction_properties=STAMP)

    restored = table.open(StorageOptions())
    root = Path(table.location.removeprefix("file://"))
    uuids = {t.uuid for t in (restored.read_transaction(v["version"]) for v in restored.versions()) if t is not None}
    left = sorted(p.name for p in (root / "_transactions").iterdir() if p.name.split("-", 1)[1].removesuffix(".txn") not in uuids)
    assert (len(lost), version, sorted(restored.to_table(columns=["id"]).column("id").to_pylist()), left) == (1, 3, [1, 2, 3], [])


def test_a_lost_finalize_whose_version_is_ambiguous_keeps_what_it_wrote(table: DoorTable, monkeypatch: pytest.MonkeyPatch) -> None:
    """A version found under both naming schemes proves no commit's manifest there ([[LH-564]]): the attempt that lost to it
    cannot tell it never landed, so it keeps its files for Lance's own rule rather than delete what a version may name."""
    finalize = table_claims.DoorTable.finalize_version
    lost: list[int] = []

    def loses_once(door: DoorTable, request: CreateTableVersionRequest, *, turn: str | None = None) -> object:
        if not lost:
            lost.append(request.version)
            raise ConcurrentModificationError("another commit took the version first")
        return finalize(door, request, turn=turn)

    def ambiguous(location: str, so: StorageOptions, version: int) -> str | None:
        return "" if version in lost else committed_manifest_digest(location, so, version)

    monkeypatch.setattr(table_claims.DoorTable, "finalize_version", loses_once)
    monkeypatch.setattr("catalog.services.dataplane.committed_manifest_digest", ambiguous)

    update_table(table, StorageOptions(), UpdateTableRequest(id=TABLE_ID, predicate="id >= 0", updates=[["id", "id + 10"]]), transaction_properties=STAMP)

    latest = table.open(StorageOptions())
    root = Path(table.location.removeprefix("file://"))
    uuids = {t.uuid for t in (latest.read_transaction(v["version"]) for v in latest.versions()) if t is not None}
    left = [p.name for p in (root / "_transactions").iterdir() if p.name.split("-", 1)[1].removesuffix(".txn") not in uuids]
    assert (len(lost), len(left)) == (1, 1), left


@pytest.mark.parametrize(
    ("refusal", "unread", "answer"),
    [
        pytest.param(ConcurrentModificationError("the store answered the put as a conflict after it landed"), False, None, id="answered-as-a-conflict"),
        pytest.param(
            ConcurrentModificationError("the store answered the put as a conflict after it landed"),
            True,
            ServiceUnavailableError,
            id="answered-as-a-conflict-and-its-landing-unread",
        ),
        pytest.param(
            table_claims.TableChangedError("the name left the location after the put landed"),
            False,
            table_claims.TableChangedError,
            id="the-table-changed-after",
        ),
    ],
)
def test_a_finalize_refused_after_its_version_landed_keeps_that_commits_record(
    table: DoorTable, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: Exception, unread: bool, answer: type[Exception] | None
) -> None:
    """ADR 0187: a finalize can be refused after its conditional put landed: the store answers it as a conflict, or the
    door finds the name left the location once it wrote. The version holds the very manifest the detached commit judged,
    so its author record is kept, and the change is applied once: a conflict is answered as that version, never planned
    again on the version it made; one whose landing the store does not answer is refused 503, its outcome unknown; and a
    changed table is refused 409 as it was."""
    finalize = table_claims.DoorTable.finalize_version
    refused_after: list[int] = []

    def landed_then_refused(door: DoorTable, request: CreateTableVersionRequest, *, turn: str | None = None) -> object:
        landed = finalize(door, request, turn=turn)
        if not refused_after:
            refused_after.append(request.version)
            raise refusal
        return landed

    def unanswered(*_args: object, **_kwargs: object) -> str | None:
        raise OSError("the store did not answer reading the version's manifest")

    monkeypatch.setattr(table_claims.DoorTable, "finalize_version", landed_then_refused)
    if unread:
        monkeypatch.setattr("catalog.services.dataplane.committed_manifest_digest", unanswered)
    request = UpdateTableRequest(id=TABLE_ID, predicate="id >= 0", updates=[["id", "id + 10"]])
    try:
        outcome: int | type[Exception] = update_table(table, StorageOptions(), request, transaction_properties={**STAMP, "rask.operation": "update"}).version
    except Exception as error:  # noqa: BLE001 — the door's answer is the outcome under test
        outcome = type(error)

    (landed,) = refused_after
    latest = table.open(StorageOptions())
    ids = sorted(latest.to_table(columns=["id"]).column("id").to_pylist())
    signed = _signed(table, str(tmp_path / "control"), table.resolved.require_uuid(), landed)
    assert (outcome, latest.version, ids, signed) == (answer or landed, landed, [11, 12, 13], ("alice", True))


def _signed(table: DoorTable, control: str, table_uuid: str, version: int) -> tuple[str, bool]:
    """The author recorded for ``version``, and whether the record names the manifest that landed there."""
    transaction = table.open(StorageOptions(), version=version).read_transaction(version)
    assert transaction is not None
    record = commit_records.recorded(control, StorageOptions(), table_uuid, transaction_uuid=transaction.uuid, version=version, ref="")
    assert record is not None, version
    return record.author, record.manifest == committed_manifest_digest(table.location, StorageOptions(), version)


#: An unkeyed table, so ``/insert`` appends through Lance's own commit rather than a stamped one of its own.
APPENDED_TO = ["db", "appended"]


def _resolved(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """``APPENDED_TO`` as a door acts on it, resolved now."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), APPENDED_TO, announces=lambda _operation: True)


def _ipc(rows: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    return sink.getvalue().to_pybytes()


def _row(row: int) -> pa.Table:
    """One appended row, lacking the nullable columns a change adds or drops: Lance fills them null (pylance 13.0.0)."""
    return pa.table({"id": pa.array([row], pa.int64()), "v": pa.array([row], pa.int64())})


def _add_a_null_column(table: DoorTable) -> int | None:
    request = AlterTableAddColumnsRequest(id=APPENDED_TO, new_columns=[AddColumnsEntry(name="n", expression="cast(NULL as string)")])
    return add_columns(table, StorageOptions(), request, transaction_properties=STAMP).version


def _drop_a_column(table: DoorTable) -> int | None:
    request = AlterTableDropColumnsRequest(id=APPENDED_TO, columns=["w"])
    return drop_columns(table, StorageOptions(), request, transaction_properties={**STAMP, "rask.operation": "drop_columns"}).version


def _update_the_first_row(table: DoorTable) -> int | None:
    request = UpdateTableRequest(id=APPENDED_TO, predicate="id = 0", updates=[["v", "7"]])
    return update_table(table, StorageOptions(), request, transaction_properties={**STAMP, "rask.operation": "update"}).version


def _delete_the_first_row(table: DoorTable) -> int | None:
    request = DeleteFromTableRequest(id=APPENDED_TO, predicate="id = 0")
    return delete_from_table(table, StorageOptions(), request, transaction_properties={**STAMP, "rask.operation": "delete"}).version


@pytest.mark.parametrize(
    ("change", "made", "kept_first_row"),
    [
        pytest.param(_add_a_null_column, lambda version: "n" in version.schema.names, True, id="a-null-column-added"),
        pytest.param(_drop_a_column, lambda version: "w" not in version.schema.names, True, id="a-column-dropped"),
        pytest.param(_update_the_first_row, lambda version: version.to_table(filter="id = 0").column("v").to_pylist() == [7], True, id="a-row-updated"),
        pytest.param(_delete_the_first_row, lambda version: version.count_rows(filter="id = 0") == 0, False, id="a-row-deleted"),
    ],
)
def test_a_commit_lands_while_insert_appends_in_a_tight_loop_and_every_append_lands(
    ns,  # noqa: ANN001
    tmp_path: Path,
    change: Callable[[DoorTable], int | None],
    made: Callable[[lance.LanceDataset], bool],
    kept_first_row: bool,
) -> None:
    """[[LH-259]] A writer the catalog's ``/insert`` commits in-process in a tight loop takes a version every few
    milliseconds, so a door's commit, finalized as exactly the version after the one it planned on, would lose every race:
    measured on pylance 13.0.0 without the turn, a ``cast(NULL)`` add answered 409 after all ten of its plans in 6 of 6
    rounds, and a drop, an update and a delete held their request 214-302 s first. Lance cannot rebase the add's ``Merge``
    over an Append at all (``lance_docs/lance/format/table/transaction.md`` § Merge Compatibility), and the drop, the update
    and the delete each lose another way: at the finalize, and inside Lance's own retries. Each takes its ref's turn after
    its first lost race, the appends wait it out, and every append still lands. The change lands within 10 s: shown the
    versions committed since its plan, Lance spends its own retries on them even with the turn (measured on pylance 13.0.0,
    a drop and a delete each took 30.4-30.5 s), so the namespace a change is planned through (``dataplane.captured``)
    answers none after the one planned on."""
    seeded = pa.table({"id": pa.array([0], pa.int64()), "v": pa.array([0], pa.int64()), "w": pa.array([0], pa.int64())})
    registry = base_registry.BaseRegistry(control_root=str(tmp_path / "control"), storage_options={})
    create_table(ns, StorageOptions(), APPENDED_TO, seeded, root=str(tmp_path / "data"), mode="create", registry=registry, holder="$".join(APPENDED_TO))
    stop = threading.Event()
    appended: list[int] = []
    failed: list[BaseException] = []

    def append() -> None:
        writer, row = _resolved(ns, tmp_path), 1
        while not stop.is_set():
            try:
                insert_into_table(
                    writer,
                    StorageOptions(),
                    InsertIntoTableRequest(id=APPENDED_TO, mode="append"),
                    _ipc(_row(row)),
                    max_bytes=1 << 20,
                    transaction_properties=STAMP,
                )
            except BaseException as exc:  # noqa: BLE001 — every append's outcome is half of the claim
                failed.append(exc)
                return
            appended.append(row)
            row += 1

    thread = threading.Thread(target=append, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while len(appended) < 3 and not failed:
        assert time.monotonic() < deadline, "the writer never got going"
        time.sleep(0.01)

    try:
        began = time.monotonic()
        version = change(_resolved(ns, tmp_path))
        took = time.monotonic() - began
    finally:
        stop.set()
        thread.join(timeout=60)

    table = _resolved(ns, tmp_path)
    ids = table.open(StorageOptions()).to_table(columns=["id"]).column("id").to_pylist()
    assert (failed, sorted(ids)) == ([], sorted([0, *appended] if kept_first_row else appended))
    assert version is not None and made(table.open(StorageOptions(), version=version))
    assert took < 10, f"the change landed after {took:.1f} s"


def test_a_column_named_concurrent_is_still_a_MISSING_COLUMN(table: DoorTable) -> None:
    """The ordering guard, and it is not hypothetical.

    `_COMMIT_CONFLICT_MARKERS` carries the bare word `concurrent`, so testing it before the
    missing-column check would report a lost race (14) for a caller who simply named a column
    `concurrent` — turning their typo into "the table changed underneath you, retry", which is advice
    that can never succeed.
    """
    from lance_namespace import AlterTableDropColumnsRequest, TableColumnNotFoundError

    from catalog.services.dataplane import drop_columns

    with pytest.raises(TableColumnNotFoundError) as caught:
        drop_columns(table, StorageOptions(), AlterTableDropColumnsRequest(id=TABLE_ID, columns=["concurrent"]), transaction_properties=STAMP)
    assert caught.value.code == 12, f"a column named 'concurrent' minted code {caught.value.code}, not 12"
