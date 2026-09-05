"""Maintenance is WORKER work: the catalog plans and commits, a worker moves the bytes.

Compaction must not run inside the catalog's request handler. Rewriting a table's data files is
unbounded, CPU- and memory-heavy work whose cost is set by the table, not by the request — running it
in-process is what pins the catalog to `replicas: 1`, makes its memory ceiling a function of the
largest table anyone owns, and turns a maintenance pass into an availability incident for every other
door on the pod. `docs/DECISIONS.md` ("The lakehouse cloud-native cutover") records this as the estate's central
non-cloud-native defect, and its resolution.

Lance ships the protocol for doing it properly, and it is a three-way split by CREDENTIAL as much as by
machine:

  1. ``Compaction.plan``            — the CATALOG, root creds. A metadata read: which fragments to merge.
  2. ``CompactionTask.execute``     — a WORKER, vended table-scoped creds. All the bytes. All the time.
  3. ``Compaction.commit``          — the CATALOG, root creds. Metadata-only; mints the new version.

Steps 1 and 3 are the only halves that need root, and neither reads a data byte. The task and the
result are both JSON strings (``.json()`` / ``.from_json()``), so the work rides a queue between them.

These pin the dataplane primitives against real pylance — no S3, no cluster — the same shape as
``test_client_direct_commit.py``, which pins the sibling client-direct append.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from lance_namespace import InvalidInputError, TableNotFoundError

from catalog.services.dataplane import commit_compaction, plan_compaction


def _table_of(n: int) -> pa.Table:
    return pa.table({"id": pa.array(range(n), pa.int64()), "v": [str(i) for i in range(n)]})


def _seed(tmp_path: Path, *, fragments: int, rows_each: int = 100) -> str:
    """A table already split across ``fragments`` data files — the shape compaction exists to fix."""
    uri = str(tmp_path / "t")
    table = _table_of(fragments * rows_each)
    lance.write_dataset(table[:rows_each], uri, data_storage_version="2.2", enable_stable_row_ids=True)
    for start in range(rows_each, fragments * rows_each, rows_each):
        lance.write_dataset(table[start : start + rows_each], uri, mode="append", data_storage_version="2.2")
    return uri


def _worker_executes(uri: str, task_json: str) -> str:
    """The WORKER half, standing in for the executor: rehydrate the task, do the bytes, hand back JSON.

    Deliberately goes nowhere near the catalog's helpers — a worker is a separate process holding only
    vended creds and the task string it read off the queue.
    """
    from lance.optimize import CompactionTask

    # pylance's ``optimize.pyi`` stops at ``execute``; ``from_json``/``json`` exist on the Rust class and
    # round-trip (verified 2026-09-03). The alias is where that stub gap is named — see
    # ``dataplane._RewriteResult`` for the production side of the same absence.
    task: Any = CompactionTask
    return str(task.from_json(task_json).execute(lance.dataset(uri)).json())


def test_the_catalog_plans_compaction_and_hands_back_queue_shippable_tasks(tmp_path: Path) -> None:
    uri = _seed(tmp_path, fragments=3)

    plan = plan_compaction(uri, {}, target_rows_per_fragment=1000)

    assert plan.read_version == lance.dataset(uri).version
    assert plan.tasks, "three 100-row fragments with a 1000-row target must plan at least one task"
    # Queue-shippable is the whole point: a task that cannot survive `json.dumps` cannot reach a worker.
    for task in plan.tasks:
        assert isinstance(task, str)
        json.loads(task)
    # Planning is a METADATA read — it must not have minted a version or moved a byte.
    assert lance.dataset(uri).version == plan.read_version


def test_a_worker_executes_the_task_and_the_catalog_commits_the_result(tmp_path: Path) -> None:
    uri = _seed(tmp_path, fragments=3)
    before = lance.dataset(uri)
    assert len(before.get_fragments()) == 3

    plan = plan_compaction(uri, {}, target_rows_per_fragment=1000)
    results = [_worker_executes(uri, task) for task in plan.tasks]
    outcome = commit_compaction(uri, {}, results)

    after = lance.dataset(uri)
    assert outcome.version == after.version > before.version
    assert outcome.fragments_removed == 3
    assert outcome.fragments_added == 1
    assert len(after.get_fragments()) == 1
    # The rows survive the rewrite intact, and so does the create-time stable-row-id config — a
    # compaction that silently dropped either would be a data-loss bug wearing a maintenance name.
    assert after.count_rows() == 300
    assert after.to_table().sort_by("id")["id"].to_pylist() == list(range(300))
    assert after.has_stable_row_ids


def test_a_policy_knob_this_door_does_not_honour_is_refused_not_dropped(tmp_path: Path) -> None:
    """Lance's option set is wider than the set this door forwards.

    Silently dropping an unrecognized knob is the dropped-parameter defect: the caller tunes a buffer
    size, the plan ignores it, and the 200 says it worked. Refuse instead, naming what is accepted.

    `io_buffer_size` is the right example precisely because it is the SHAPE of knob this door does
    forward two of — a machine-level bound. The two that cross (`batch_size`, `num_threads`) do so on
    a measured necessity: Lance bakes them into the task at plan time and gives the executor no later
    chance to set them. `io_buffer_size` is baked the same way but nothing in this estate configures
    it, and forwarding a knob nobody sets would widen the door for no reason.
    """
    uri = _seed(tmp_path, fragments=2)
    with pytest.raises(InvalidInputError) as caught:
        plan_compaction(uri, {}, io_buffer_size=8192)
    assert "io_buffer_size" in str(caught.value)


def test_a_table_that_needs_no_compaction_plans_no_work(tmp_path: Path) -> None:
    # One fragment already at target: the plan is empty, and an empty plan must be an ANSWER (no work
    # to queue), not an error — otherwise a scheduled sweep pages an operator for a healthy table.
    uri = _seed(tmp_path, fragments=1)
    assert plan_compaction(uri, {}, target_rows_per_fragment=1000).tasks == []


def test_a_result_from_a_stale_plan_is_refused_as_non_retryable(tmp_path: Path) -> None:
    """A worker's result that lost a race to an Overwrite describes data the table no longer has.

    Lance refuses it (``Incompatible transaction``) and the door must relay that as a client error the
    caller is told NOT to retry — re-committing the same result after a re-plan is how a compaction
    resurrects overwritten data.
    """
    uri = _seed(tmp_path, fragments=3)
    plan = plan_compaction(uri, {}, target_rows_per_fragment=1000)
    results = [_worker_executes(uri, task) for task in plan.tasks]
    # Someone overwrites the table while the worker was busy.
    lance.write_dataset(_table_of(5), uri, mode="overwrite", data_storage_version="2.2")

    with pytest.raises(InvalidInputError) as caught:
        commit_compaction(uri, {}, results)
    message = str(caught.value).lower()
    assert "not retryable" in message
    # The remedy must name the work THIS caller has to redo. The sibling append door tells its caller to
    # re-WRITE the data; a compaction worker wrote no data of its own, and sending it back to re-write is
    # both impossible and a distraction from the plan that actually went void.
    assert "re-plan" in message
    assert "re-write the data" not in message
    assert lance.dataset(uri).count_rows() == 5, "the refused commit must leave the table untouched"


def test_malformed_worker_output_is_a_client_error_not_a_crash(tmp_path: Path) -> None:
    # The result is client-controlled input off a queue. `RewriteResult.from_json` raises ValueError on
    # a missing field; that must reach the caller as a 400, never a 500.
    uri = _seed(tmp_path, fragments=2)
    with pytest.raises(InvalidInputError):
        commit_compaction(uri, {}, ['{"nope": 1}'])


def test_an_empty_result_set_is_refused_rather_than_reported_as_a_no_op_success(tmp_path: Path) -> None:
    """Lance answers an empty rewrite list with a silent zero-metric success and no new version.

    Relayed verbatim that reads as "compaction ran, nothing needed doing" — indistinguishable from a
    healthy table, and it would mask an executor that lost every result it was handed. A caller with
    nothing to commit has no business at this door.
    """
    uri = _seed(tmp_path, fragments=3)
    with pytest.raises(InvalidInputError):
        commit_compaction(uri, {}, [])


def test_the_worker_writes_the_bytes_before_the_catalog_is_asked_to_commit(tmp_path: Path) -> None:
    """The commit is metadata-only: every data file it publishes already exists when it is called.

    This is the property that lets the catalog stay small. If the commit half were still moving bytes,
    the new file would appear DURING the call rather than before it.
    """
    uri = _seed(tmp_path, fragments=3)
    data_dir = Path(uri) / "data"
    before = {p.name for p in data_dir.iterdir()}

    plan = plan_compaction(uri, {}, target_rows_per_fragment=1000)
    results = [_worker_executes(uri, task) for task in plan.tasks]
    written_by_the_worker = {p.name for p in data_dir.iterdir()} - before
    assert written_by_the_worker, "the worker's execute() is what writes the compacted data file"

    commit_compaction(uri, {}, results)
    assert {p.name for p in data_dir.iterdir()} - before == written_by_the_worker


def test_the_executors_MEMORY_BOUNDS_survive_the_plan_because_the_task_bakes_them(tmp_path: Path) -> None:
    """The plan door must forward `batch_size`/`num_threads`, and the reason is a measured fact.

    They read like machine knobs the executor should own, and the door refused them on exactly that
    reasoning. Lance gives the executor no later chance to state them: measured on pylance 10.0.0
    (2026-09-04), a planned task's JSON carries an `options` object holding both, and
    `CompactionTask.execute(dataset)` accepts no options at all. A plan made without them condemns
    every distributed rewrite to Lance's defaults — an 8192-ROW batch, which against ~1.8 MB bronze
    page-image rows is ~15 GB per compute thread, times a thread count taken from the HOST's cores
    rather than the pod's limit.

    That is the unbounded read the maintenance plane already bounds for the in-pod path
    (MAINTENANCE_SCAN_BATCH_SIZE=64, MAINTENANCE_COMPACT_THREADS=2). Refusing them here would make
    the distributed path the one route in the estate that cannot be bounded at all.
    """
    uri = _seed(tmp_path, fragments=3)

    planned = plan_compaction(uri, {}, target_rows_per_fragment=1024, batch_size=64, num_threads=2)

    assert planned.tasks, "the fixture must have something to compact or this proves nothing"
    baked = json.loads(planned.tasks[0])["options"]
    assert baked["batch_size"] == 64, f"the executor's read bound did not reach the task: {baked}"
    assert baked["num_threads"] == 2, f"the executor's thread bound did not reach the task: {baked}"


def test_a_plan_made_WITHOUT_the_bounds_runs_on_lances_defaults(tmp_path: Path) -> None:
    """The other half of the same fact, stated so a future reader does not have to re-measure it: a
    task planned without the bounds carries nulls, and nothing downstream can fill them in."""
    uri = _seed(tmp_path, fragments=3)

    planned = plan_compaction(uri, {}, target_rows_per_fragment=1024)

    baked = json.loads(planned.tasks[0])["options"]
    assert baked["batch_size"] is None and baked["num_threads"] is None


def test_a_table_whose_BYTES_are_missing_is_a_client_error_not_a_500(tmp_path: Path) -> None:
    """A declared-or-registered table that was never written must say so.

    MEASURED LIVE 2026-09-04, which is how it was found: a table declared into a namespace bound to
    one warehouse, with its data written to another bucket, reached `lance.dataset()` and pylance's
    "Dataset at path ... was not found" escaped as a bare 500 "Internal Server Error". That tells an
    operator nothing and reads as a catalog fault rather than as a table nobody wrote — and the
    distinction is the whole diagnosis.

    `TableNotFoundError` (code 3, HTTP 404) rather than `InvalidInputError` (code 13, HTTP 400):
    clients dispatch on the CODE, and 13 says the REQUEST is malformed when nothing about the policy
    is. What is absent is the data, which is the answer `rename_table` already gives for a source
    that resolves to nothing — one client branch for one condition.
    """
    with pytest.raises(TableNotFoundError, match="never written"):
        plan_compaction(str(tmp_path / "nothing-here"), {}, target_rows_per_fragment=1024)
