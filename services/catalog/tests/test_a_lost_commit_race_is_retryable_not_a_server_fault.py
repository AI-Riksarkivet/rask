"""A column op that loses a commit race must say so — code 14, not Internal 18.

the lakehouse register, row A5 (drained 2026-09-10; in git history), the "column ops never mint 14" half.

Lance calls this failure RETRYABLE in its own message, and the spec has a code that says exactly that:
14 `ConcurrentModification` (409). Reported as 18 instead, the two things a caller should do become
impossible — a client cannot know to re-read and re-commit, and an operator is paged for contention
that resolves itself. It is the one error class here that is nobody's mistake.

MEASURED 2026-09-07: six concurrent `add_columns` against one table, opened at the same version. Five
lose with ``OSError("Retryable commit conflict for version 2: This Merge transaction was preempted by
concurrent transaction ...")``. `_column_op` matched none of its markers, so all five answered 500.

The vocabulary is NOT new — `_COMMIT_CONFLICT_MARKERS` already existed and already meant this, and
`_classify_commit_error` already mints 14 from it for the commit door. The column ops simply never
asked. So the fix is one branch in the one guard all four column ops share, reusing the tuple rather
than minting a second spelling of "conflict".

THE RACE IS REAL, NOT SIMULATED. Six threads commit conflicting schema changes against one dataset;
this asserts on the losers. It is not marked slow — measured at well under a second, because the
conflict is detected at commit rather than after any large write.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import ConcurrentModificationError, connect

from catalog.services.dataplane import add_columns, create_table


lance = pytest.importorskip("lance")

from lance_namespace import AlterTableAddColumnsRequest  # noqa: E402
from lance_namespace_urllib3_client.models.add_columns_entry import AddColumnsEntry  # noqa: E402


TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64())])

#: Enough writers that at least one must lose. Every one of them opens at the same version and commits
#: an incompatible schema change, so exactly one can win by construction.
WRITERS = 6


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, SCHEMA) as writer:
        writer.write_table(pa.table({"id": pa.array([1, 2, 3], pa.int64())}, schema=SCHEMA))
    create_table(namespace, {}, TABLE_ID, sink.getvalue().to_pybytes(), mode="create")
    return namespace


def test_the_losers_of_an_add_columns_race_answer_CONCURRENT_MODIFICATION(ns) -> None:  # noqa: ANN001
    """The headline: a lost race is 14, and a caller can act on 14."""

    def add(i: int) -> BaseException | None:
        request = AlterTableAddColumnsRequest(id=TABLE_ID, new_columns=[AddColumnsEntry(name=f"c{i}", expression="id + 1")])
        try:
            add_columns(ns, {}, request)
        except BaseException as exc:  # noqa: BLE001 — the losers are the subject; every one is inspected
            return exc
        return None

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        outcomes = [o for o in pool.map(add, range(WRITERS)) if o is not None]

    assert outcomes, f"none of {WRITERS} concurrent add_columns lost — the race did not happen, so this proves nothing"
    for exc in outcomes:
        assert isinstance(exc, ConcurrentModificationError), f"a lost commit race answered {type(exc).__name__}: {exc}"
        assert exc.code == 14, f"a lost commit race minted code {exc.code}, not 14"


def test_a_column_named_concurrent_is_still_a_MISSING_COLUMN(ns) -> None:  # noqa: ANN001
    """The ordering guard, and it is not hypothetical.

    `_COMMIT_CONFLICT_MARKERS` carries the bare word `concurrent`, so testing it before the
    missing-column check would report a lost race (14) for a caller who simply named a column
    `concurrent` — turning their typo into "the table changed underneath you, retry", which is advice
    that can never succeed.
    """
    from lance_namespace import AlterTableDropColumnsRequest, TableColumnNotFoundError

    from catalog.services.dataplane import drop_columns

    with pytest.raises(TableColumnNotFoundError) as caught:
        drop_columns(ns, {}, AlterTableDropColumnsRequest(id=TABLE_ID, columns=["concurrent"]))
    assert caught.value.code == 12, f"a column named 'concurrent' minted code {caught.value.code}, not 12"
