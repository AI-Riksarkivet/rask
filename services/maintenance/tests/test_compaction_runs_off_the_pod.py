"""The rewrite's BYTES leave the pod that plans and commits them — `open_maintenance_compute.md` M2.

The defect this closes: `compact_files()` does all three phases in one process, so the maintenance
pod's memory ceiling is a function of the largest table anyone owns. Lance ships the split precisely
so they can run apart — `Compaction.plan` (metadata, cheap, transactional) → `CompactionTask.execute`
(IO-bound, unbounded in the data) → `Compaction.commit` (metadata again) — and the catalog has served
both metadata halves since the cloud-native cutover with nothing consuming them.

MEASURED, not assumed, before this was built (pylance 10.0.0, 2026-09-04):

* the four serialization halves all exist and round-trip — `CompactionTask.json`/`from_json`,
  `RewriteResult.json`/`from_json` — so only JSON crosses the seam;
* a PARTIAL set of a plan's results commits cleanly (6 tasks planned, 1 committed: 12 fragments → 11,
  120 rows intact), which is what licenses committing what succeeded when one task fails rather than
  orphaning bytes that are already written;
* a sibling result committed AFTER that partial commit is still accepted, rows intact — so the
  remainder is not stranded by an earlier commit of the same plan.

The credential split is the point of the exercise: plan and commit are the catalog's, signed by the
key that may read every manifest; execute is the worker's, signed by a credential scoped to the one
table it rewrites and expiring in 900s.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from maintenance.services import compaction_executor as ce


def _fragmented(tmp_path: Path, *, writes: int = 6, rows: int = 10, table_id: str | None = None) -> str:
    """A dataset with something to compact. Separate writes, because one write is one fragment.

    ``table_id`` stamps the producer's canonical name into the schema metadata — the same key
    `medallion.services.compute` writes and `maintenance.core.lineage_emit.declared_table_id` reads.
    Without it a dataset is unplannable (the catalog's doors are addressed by table identifier), so
    the distributed branch is never entered — which is a real behaviour and also the way a test of
    that branch can pass for entirely the wrong reason.
    """
    uri = str(tmp_path / "t.lance")
    schema_metadata = {b"lineage.dataset_id": table_id.encode()} if table_id else None
    for i in range(writes):
        table = pa.table({"id": pa.array([i * rows + j for j in range(rows)], pa.int64())})
        if schema_metadata is not None:
            table = table.replace_schema_metadata(schema_metadata)
        lance.write_dataset(table, uri, mode="create" if i == 0 else "append")
    return uri


class _Catalog:
    """The catalog's two metadata halves, in-process — the doors exist and are tested in their own
    suite; what this module must prove is that the WORKER drives them correctly."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.planned: list[dict[str, Any]] = []
        self.committed: list[list[str]] = []

    def plan(self, table_id: str, policy: dict[str, Any]) -> ce.PlannedWork:
        from catalog.services.dataplane import plan_compaction

        self.planned.append({"table_id": table_id, "policy": policy})
        plan = plan_compaction(self.uri, {}, **policy)
        return ce.PlannedWork(read_version=plan.read_version, tasks=plan.tasks)

    def commit(self, table_id: str, results: list[str]) -> ce.CommittedWork:
        from catalog.services.dataplane import commit_compaction

        self.committed.append(list(results))
        outcome = commit_compaction(self.uri, {}, results)
        return ce.CommittedWork(
            version=outcome.version,
            fragments_added=outcome.fragments_added,
            fragments_removed=outcome.fragments_removed,
        )


def test_the_bytes_are_rewritten_where_the_plan_was_not(tmp_path: Path) -> None:
    """The headline: fragments merge, and no data byte passed through the planning process."""
    uri = _fragmented(tmp_path)
    catalog = _Catalog(uri)
    assert len(lance.dataset(uri).get_fragments()) == 6

    outcome = ce.compact_distributed(
        uri,
        table_id="acme-bronze$events",
        write_options={},
        plan=catalog.plan,
        commit=catalog.commit,
        policy={"target_rows_per_fragment": 1024},
    )

    assert outcome is not None
    assert outcome.tasks_executed >= 1
    assert outcome.fragments_removed == 6
    after = lance.dataset(uri)
    assert len(after.get_fragments()) == 1, "the rewrite did not land"
    assert after.count_rows() == 60, "compaction lost rows"


def test_a_table_already_at_TARGET_is_a_successful_no_op(tmp_path: Path) -> None:
    """An empty plan is the ANSWER, not a fault. A sweep over a healthy estate must be able to
    conclude "nothing to do" without paging anyone — and must not call commit, which refuses an empty
    result list precisely so a worker that lost every result cannot look like a clean table."""
    uri = _fragmented(tmp_path, writes=1)
    catalog = _Catalog(uri)

    outcome = ce.compact_distributed(uri, table_id="acme-bronze$events", write_options={}, plan=catalog.plan, commit=catalog.commit, policy={})

    assert outcome is not None
    assert outcome.tasks_executed == 0
    assert catalog.committed == [], "an empty plan must not reach the commit door"


def test_the_EXECUTE_credential_is_the_one_this_worker_was_given(tmp_path: Path) -> None:
    """The credential split, asserted at the only seam where it is decidable.

    Plan and commit are the catalog's calls, signed by the key that may read every manifest — spying
    on `lance.dataset` itself would catch those too and prove nothing. `_open_for_rewrite` is the ONE
    open this module makes, so holding it still is the exact question: does the rewrite go through
    the vended, table-scoped options it was handed, or through an ambient credential?
    """
    uri = _fragmented(tmp_path)
    catalog = _Catalog(uri)
    vended = {"aws_access_key_id": "vended", "aws_secret_access_key": "scoped"}
    seen: list[dict[str, str]] = []
    real = ce._open_for_rewrite

    def _spy(location: str, write_options: Any) -> Any:
        seen.append(dict(write_options))
        return real(location, {})  # opened for real against the local fixture; the OPTIONS are the assertion

    ce._open_for_rewrite = _spy  # type: ignore[assignment]
    try:
        ce.compact_distributed(
            uri,
            table_id="acme-bronze$events",
            write_options=vended,
            plan=catalog.plan,
            commit=catalog.commit,
            policy={"target_rows_per_fragment": 1024},
        )
    finally:
        ce._open_for_rewrite = real  # type: ignore[assignment]

    assert seen == [vended], f"the rewrite opened with {seen}, not the credential it was handed"


def test_one_failed_task_COMMITS_the_rest_rather_than_orphaning_it(tmp_path: Path) -> None:
    """Measured policy, not a preference: the successful tasks' files are already written.

    Discarding them leaves bytes on the store for the orphan sweep to find — a whole-bucket scan with
    an age threshold — and repeats the work next tick. A partial commit was verified to be a valid,
    lossless compaction, so committing what landed is strictly better. The failure is still reported,
    or a half-done pass would read as a clean one.
    """
    uri = _fragmented(tmp_path, writes=12)
    catalog = _Catalog(uri)
    calls = {"n": 0}
    real_execute = ce._execute_one

    def _flaky(task_json: str, dataset: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("worker OOM on task 2")
        return real_execute(task_json, dataset)

    ce._execute_one = _flaky  # type: ignore[assignment]
    try:
        outcome = ce.compact_distributed(
            uri,
            table_id="acme-bronze$events",
            write_options={},
            plan=catalog.plan,
            commit=catalog.commit,
            policy={"target_rows_per_fragment": 20},
        )
    finally:
        ce._execute_one = real_execute  # type: ignore[assignment]

    assert outcome is not None
    assert outcome.tasks_failed == 1, "the failure must be reported, or a half-done pass reads as clean"
    assert outcome.tasks_executed >= 1
    assert catalog.committed and catalog.committed[0], "the successful rewrites were discarded"
    assert lance.dataset(uri).count_rows() == 120, "a partial commit lost rows"


def test_EVERY_task_failing_commits_NOTHING(tmp_path: Path) -> None:
    """The commit door refuses an empty result list, and calling it anyway would turn a total worker
    failure into a 400 that reads like a malformed request rather than what it is."""
    uri = _fragmented(tmp_path)
    catalog = _Catalog(uri)
    real_execute = ce._execute_one

    def _always_fails(task_json: str, dataset: Any) -> str:
        raise RuntimeError("no capacity")

    ce._execute_one = _always_fails  # type: ignore[assignment]
    try:
        with pytest.raises(ce.DistributedCompactionError, match="no task"):
            ce.compact_distributed(uri, table_id="acme-bronze$events", write_options={}, plan=catalog.plan, commit=catalog.commit, policy={})
    finally:
        ce._execute_one = real_execute  # type: ignore[assignment]

    assert catalog.committed == []


# --- the wiring: the sweep must actually reach for it -----------------------------------------------


def _settings(**over: object) -> Any:
    from maintenance.core.config import MaintenanceSettings

    return MaintenanceSettings.model_validate({"s3_bucket": "lake"} | over)


def test_the_off_pod_path_is_OFF_unless_both_halves_are_configured() -> None:
    """The flag alone would produce a worker that plans against nothing.

    Plan and commit are the catalog's own doors, so a deployment with no catalog URL cannot do this
    at all — and must get exactly the in-pod rewrite it had, rather than a per-dataset failure.
    """
    from maintenance.services import sweep

    assert sweep._rewriter(_settings(), {}) is None, "off by default"
    assert sweep._rewriter(_settings(distributed_compaction=True), {}) is None, "no catalog URL — nothing to plan against"
    assert sweep._rewriter(_settings(catalog_url="http://catalog:2333"), {}) is None, "not opted in"
    assert sweep._rewriter(_settings(distributed_compaction=True, catalog_url="http://catalog:2333"), {}) is not None


def test_a_dataset_the_catalog_cannot_NAME_stays_on_the_in_pod_rewrite(tmp_path: Path) -> None:
    """`table_id` is the producer's stamp, and most of the estate's roots do not carry a derivable one.

    A dataset with no declared id cannot be planned (the doors are addressed by table identifier), so
    it must fall through to the in-pod path rather than fail — and `compaction_mode` must say so, or
    an estate could run every compaction on the path it configured away from with nothing to read.
    """
    from maintenance.services.optimize import compact_one

    uri = _fragmented(tmp_path)
    called = {"n": 0}

    def _never(uri: str, *, table_id: str, options: Any) -> Any:
        called["n"] += 1
        raise AssertionError("a dataset with no declared table id must not be planned")

    result = compact_one(uri, {}, None, target_rows_per_fragment=1024, cleanup_enabled=False, optimize_indices_enabled=False, rewrite=_never)

    assert called["n"] == 0
    assert result.compaction_mode == "in_pod"
    assert result.error is None, result.error
    assert len(lance.dataset(uri).get_fragments()) == 1, "the in-pod rewrite did not run"


def test_an_UNAVAILABLE_plan_door_falls_back_and_the_result_SAYS_which_path_ran(tmp_path: Path) -> None:
    """Nothing was planned, so nothing was written: falling back is safe and keeps disk being
    reclaimed. What must not happen is the fallback being invisible."""
    from maintenance.services.optimize import compact_one

    # STAMPED, so the distributed branch is actually entered — without a declared id the dataset is
    # unplannable and this would pass without the fallback ever being reached.
    uri = _fragmented(tmp_path, table_id="acme-bronze$events")
    reached = {"n": 0}

    def _unavailable(uri: str, *, table_id: str, options: Any) -> Any:
        reached["n"] += 1
        raise ce.CompactionPlaneUnavailable("catalog is down")

    result = compact_one(uri, {}, None, target_rows_per_fragment=1024, cleanup_enabled=False, optimize_indices_enabled=False, rewrite=_unavailable)

    assert reached["n"] == 1, "the distributed branch was never entered — the fallback is untested"
    assert result.compaction_mode == "in_pod"
    assert len(lance.dataset(uri).get_fragments()) == 1, "the fallback did not compact"


def test_a_task_failure_after_planning_does_NOT_fall_back(tmp_path: Path) -> None:
    """The other side of the fallback line, and the one that matters.

    Once a task has run, bytes may already be written. Retrying the same fragments in-pod would
    rewrite them again and orphan the first set, and would report success doing it — so a failure
    past the plan must reach the sweep's per-dataset error capture rather than be papered over.
    """
    from maintenance.services.optimize import compact_one

    uri = _fragmented(tmp_path, table_id="acme-bronze$events")

    def _attempted_and_failed(uri: str, *, table_id: str, options: Any) -> Any:
        raise ce.DistributedCompactionError("no task of 3 completed")

    result = compact_one(uri, {}, None, target_rows_per_fragment=1024, cleanup_enabled=False, optimize_indices_enabled=False, rewrite=_attempted_and_failed)

    assert result.error is not None and "no task" in result.error, "the failure must be reported, not fallen back from"
    assert result.compaction_mode == "in_pod", "the mode field records what actually rewrote bytes, and nothing did"
    assert len(lance.dataset(uri).get_fragments()) == 6, "an in-pod rewrite ran after a distributed attempt already moved bytes"
