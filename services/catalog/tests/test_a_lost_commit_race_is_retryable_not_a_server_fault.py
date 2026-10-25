"""A column op racing another commit lands, planned again on the version that won; none answers a server fault.

Every column op commits stamped ([[LH-497]], `dataplane.commit_stamped`): Lance's own operation is planned on the
latest version and finalized as exactly the next one through the backend's put-if-not-exists, and a writer that lost
that version plans again on the new latest. So six concurrent `add_columns` on one table, each adding its own column,
all land, in six consecutive versions. A writer losing every one of its attempts answers 14 `ConcurrentModification`
(409), the spec's code for a retryable race.

THE RACE IS REAL, NOT SIMULATED. Six threads commit schema changes against one dataset. It is not marked slow:
measured at well under a second, because nothing large is written.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import ConcurrentModificationError, CreateNamespaceRequest, connect

from catalog.services import table_claims
from catalog.services.dataplane import add_columns, create_table
from catalog.services.table_claims import DoorTable
from service_kit.lakehouse.location_claims import ClaimStore


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
        namespace, {}, TABLE_ID, pa.table({"id": pa.array([1, 2, 3], pa.int64())}, schema=SCHEMA), root=str(tmp_path / "data"), mode="create", registry=None
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
            return add_columns(table, {}, request, transaction_properties=STAMP).version
        except BaseException as exc:  # noqa: BLE001 — every writer's outcome is inspected
            return exc

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        outcomes = list(pool.map(add, range(WRITERS)))

    failed = [o for o in outcomes if isinstance(o, BaseException)]
    assert all(isinstance(o, ConcurrentModificationError) and o.code == 14 for o in failed), f"a racing add_columns answered {failed!r}"
    landed = sorted(o for o in outcomes if isinstance(o, int))
    assert landed == list(range(2, 2 + len(landed))), landed
    assert len(landed) == WRITERS, f"{len(failed)} of {WRITERS} writers lost all their attempts: {failed!r}"
    names = set(table.open({}).schema.names)
    assert {f"c{i}" for i in range(WRITERS)} <= names, names


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
        drop_columns(table, {}, AlterTableDropColumnsRequest(id=TABLE_ID, columns=["concurrent"]), transaction_properties=STAMP)
    assert caught.value.code == 12, f"a column named 'concurrent' minted code {caught.value.code}, not 12"
