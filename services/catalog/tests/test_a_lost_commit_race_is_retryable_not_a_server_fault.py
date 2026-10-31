"""A door's commit racing another commit lands, planned again on the version that won; none answers a server fault.

Every column op commits stamped ([[LH-497]], `dataplane.commit_stamped`): Lance's own operation is planned on the
latest version and finalized as exactly the next one through the backend's put-if-not-exists, and a writer that lost
that version plans again on the new latest. So six concurrent `add_columns` on one table, each adding its own column,
all land, in six consecutive versions. A writer losing every one of its attempts answers 14 `ConcurrentModification`
(409), the spec's code for a retryable race. A commit that lost its race takes its ref's turn ([[LH-259]],
`service_kit.lakehouse.commit_turns`), so it lands even beside a writer appending in a tight loop.

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
    DeleteFromTableRequest,
    InsertIntoTableRequest,
    UpdateTableRequest,
    connect,
)

from catalog.services import table_claims
from catalog.services.dataplane import add_columns, create_table, delete_from_table, drop_columns, insert_into_table, update_table
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.objectfs import StorageOptions


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
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), TABLE_ID)


def test_concurrent_add_columns_each_land_in_a_version_of_its_own(table: DoorTable) -> None:
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


#: An unkeyed table, so ``/insert`` appends through Lance's own commit rather than a stamped one of its own.
APPENDED_TO = ["db", "appended"]


def _resolved(ns, tmp_path: Path) -> DoorTable:  # noqa: ANN001
    """``APPENDED_TO`` as a door acts on it, resolved now."""
    return table_claims.door_table(ns, ClaimStore(control_root=str(tmp_path / "control")), APPENDED_TO)


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
