"""UPDATE and DELETE do not lose commit races — Lance rebases them, so there is nothing to classify.

`open_lakehouse_diff_left.md` § B3 reads "update/delete/column ops let a Lance conflict escape as 5xx".
The COLUMN half was real and landed (`test_a_lost_commit_race_is_retryable_not_a_server_fault.py`:
six concurrent `add_columns`, five losers, all answering 500 until `_column_op` learned the markers).
The update/delete half is REFUTED, measured 2026-09-08 rather than reasoned about.

Column ops are Merge transactions and genuinely conflict. `update` and `delete` are Rewrite
transactions that Lance retries internally against the newer version, so a writer holding a stale
handle does not lose — it rebases and commits. Probed directly, two handles opened at the same
version, one committing first:

    both at version 1
    A committed -> 2
    stale update: no conflict raised, version now 3
    stale delete: no conflict raised, version now 4

So adding conflict translation to `update_table`/`delete_from_table` would be a branch no input can
reach — a control that looks like one and is decoration, which is the shape this estate keeps paying
for. THIS FILE IS THE PIN ON THE REFUTATION: if Lance ever stops rebasing, these tests fail, and that
failure is the signal to add the classification the row asked for.

What DOES have to keep working is the ordering guard below — a caller's bad predicate must stay their
mistake, not become "the table moved, retry", which is advice they can never satisfy.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest
from lance_namespace import connect

from catalog.services.dataplane import create_table


lance = pytest.importorskip("lance")

from lance_namespace import DeleteFromTableRequest, InvalidInputError, UpdateTableRequest  # noqa: E402

from catalog.services.dataplane import delete_from_table, update_table  # noqa: E402


TABLE_ID = ["rows"]
SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.int64())])


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, SCHEMA) as writer:
        writer.write_table(pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": pa.array([0, 0, 0], pa.int64())}, schema=SCHEMA))
    create_table(namespace, {}, TABLE_ID, sink.getvalue().to_pybytes(), mode="create")
    return namespace


def test_a_stale_UPDATE_rebases_instead_of_conflicting(tmp_path: Path) -> None:
    """Driven at the Lance layer, because that is where the claim lives: the door cannot classify an
    error the format never raises."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": pa.array([0, 0, 0], pa.int64())}), uri)
    winner = lance.dataset(uri)
    winner.update({"v": "1"})

    stale = lance.dataset(uri, version=1)
    stale.update({"v": "9"})
    assert stale.version > winner.version, "a stale update neither conflicted nor advanced — the premise of this pin changed"


def test_a_stale_DELETE_rebases_instead_of_conflicting(tmp_path: Path) -> None:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": pa.array([0, 0, 0], pa.int64())}), uri)
    winner = lance.dataset(uri)
    winner.update({"v": "1"})

    stale = lance.dataset(uri, version=1)
    stale.delete("id = 2")
    assert stale.version > winner.version, "a stale delete neither conflicted nor advanced — the premise of this pin changed"


def test_the_doors_still_answer_and_advance_the_version(ns) -> None:  # noqa: ANN001
    """The doors are exercised too, so this file fails if they break for any reason — not only if
    Lance's conflict behaviour changes."""
    updated = update_table(ns, {}, UpdateTableRequest(id=TABLE_ID, updates=[["v", "7"]]))
    assert updated.updated_rows == 3, f"update reported {updated.updated_rows} rows, not 3"
    deleted = delete_from_table(ns, {}, DeleteFromTableRequest(id=TABLE_ID, predicate="id = 1"))
    # `version` is Optional on the response model; a door that answers None reported no version at all,
    # which is the same defect as reporting a stale one and is asserted rather than narrowed away.
    assert deleted.version is not None, "delete answered no version"
    assert deleted.version > updated.version, "delete did not advance the table version"


def test_a_bad_predicate_is_still_the_CALLER_s_mistake(ns) -> None:  # noqa: ANN001
    """The ordering guard that must survive any future conflict translation: an unknown column is a
    400 naming the valid fields, never a "retry, the table moved" 409 the caller can never satisfy."""
    with pytest.raises(InvalidInputError):
        delete_from_table(ns, {}, DeleteFromTableRequest(id=TABLE_ID, predicate="no_such_column = 1"))
