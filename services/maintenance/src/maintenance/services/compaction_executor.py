"""Compaction whose BYTES are rewritten outside the process that plans and commits them — M2.

`open_maintenance_compute.md`. `ds.optimize.compact_files()` does all three phases in one call, so
this pod's memory ceiling is a function of the largest table anyone owns rather than of its request
rate. Lance ships the split for exactly this reason and the catalog has served both metadata halves
since the cloud-native cutover with nothing consuming them:

* **plan** — a manifest read. No data byte, no new version, cheap enough for a request handler;
* **execute** — the whole rewrite, IO-bound and unbounded in the data;
* **commit** — metadata again, transactional, under the key that may write the manifest.

**The split is by CREDENTIAL, which is what makes it more than a memory fix.** Plan and commit are
the catalog's, signed by a key that reaches every manifest. Execute is the only phase this module
performs, and it opens the dataset with the options it was HANDED — a vended credential scoped to the
one table, expiring in 900s (`services/credentials.py`). A rewrite is a write, and this is the write.

**Only JSON crosses the seam**, so nothing here has to be co-located with anything: a task is an
opaque string this module hands to Lance and never parses. That is what makes M3 — submitting the
same tasks as a `RayJob` — a change of transport rather than of protocol.

Measured against pylance 10.0.0 (2026-09-04), because the failure policy below depends on all three:

* the four serialization halves round-trip (`CompactionTask.json`/`from_json`,
  `RewriteResult.json`/`from_json`);
* a PARTIAL set of a plan's results commits cleanly — 6 tasks planned, 1 committed, 12 fragments → 11
  with all 120 rows intact;
* a sibling result committed after that partial commit is still accepted, rows intact.

All IO is blocking; callers threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, cast

import lance
import lance.optimize as lance_optimize
from pydantic import BaseModel, ConfigDict


log = logging.getLogger(__name__)

#: pylance's stub stops at `execute`/`plan`/`commit`, declaring neither `CompactionTask.from_json`
#: nor `RewriteResult.json` — both exist on the Rust classes and round-trip (verified 2026-09-04).
#: Two named aliases absorb that gap, narrow enough that everything else here stays checked. The
#: catalog's `dataplane` holds the mirror pair for the same reason.
_CompactionTask: Any = lance_optimize.CompactionTask


class PlannedWork(BaseModel):
    """What the planner answered: the version the tasks were chosen against, and the tasks.

    Each task is Lance's own serialized `CompactionTask` — opaque here by design. A worker that
    parsed one would have to be re-taught on every format change, and it has no decision to make
    about fragments or encodings anyway.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    read_version: int
    tasks: list[str]


class CommittedWork(BaseModel):
    """What the commit minted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    fragments_added: int
    fragments_removed: int


class DistributedOutcome(BaseModel):
    """One dataset's distributed compaction, including what did NOT work.

    `tasks_failed` is carried rather than raised whenever at least one task landed, so a partially
    completed pass is neither reported as clean nor thrown away — see the failure policy on
    :func:`compact_distributed`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    read_version: int
    tasks_planned: int
    tasks_executed: int
    tasks_failed: int = 0
    version: int = 0
    fragments_added: int = 0
    fragments_removed: int = 0


class DistributedCompactionError(RuntimeError):
    """Work was ATTEMPTED and could not complete. The caller must not fall back.

    A total worker failure that silently became an in-pod compaction would restore the memory ceiling
    this module exists to remove, and report success doing it. Worse, a commit failure means data
    files are already written and unreferenced — retrying the whole thing in-pod would rewrite the
    same fragments again and leave the first set orphaned.
    """


class CompactionPlaneUnavailable(DistributedCompactionError):
    """The distributed path could not be STARTED — nothing was planned, nothing was written.

    A subclass so a caller that does not care catches one type, and a caller that does can fall back
    to the in-pod rewrite safely. The distinction is decided by whether a byte moved, which is the
    only line along which a fallback is sound: this is the same stance `credentials.py` takes on
    vending, where a hardening that could fail a maintenance run would be a new way to stop
    reclaiming disk.
    """


#: The planner and committer, as CALLABLES rather than a client this module constructs. The two
#: metadata halves are the catalog's HTTP doors in production and in-process functions in a test, and
#: neither is this module's business — what it owns is the execute phase and the ordering. It also
#: means M3 replaces one callable rather than rewriting this file.
Planner = Callable[[str, dict[str, Any]], PlannedWork]
Committer = Callable[[str, list[str]], CommittedWork]


def _open_for_rewrite(uri: str, write_options: Mapping[str, str]) -> lance.LanceDataset:
    """Open the dataset the rewrite will write through, with the credential this worker was HANDED.

    Its own function because it is the whole credential half of the split, and a seam a test can hold
    still: every other `lance.dataset` in a compaction belongs to the catalog's two metadata phases
    and is signed by the key those need. Opened ONCE for all tasks — re-opening per fragment group
    would re-read the manifest each time, and would be exactly where an ambient credential could
    creep back in unnoticed.
    """
    return lance.dataset(uri, storage_options=dict(write_options) or None)


def _execute_one(task_json: str, dataset: lance.LanceDataset) -> str:
    """Run one planned task and return its serialized result.

    Its own function so a failure can be attributed to ONE task — the whole point of the grain — and
    so a test can make a single task fail without stubbing the protocol.
    """
    return cast(str, _CompactionTask.from_json(task_json).execute(dataset).json())


def compact_distributed(
    uri: str,
    *,
    table_id: str,
    write_options: Mapping[str, str],
    plan: Planner,
    commit: Committer,
    policy: Mapping[str, Any],
) -> DistributedOutcome | None:
    """Plan elsewhere, rewrite here, commit elsewhere. ``None`` means this path is unavailable.

    **The failure policy is measured, not preferred.** When some tasks fail and some succeed, the
    successful ones' data files are ALREADY WRITTEN. Discarding them leaves bytes on the store for
    the orphan sweep — a whole-bucket scan with an age threshold — and repeats the work next tick. A
    partial commit was verified to be a valid, lossless compaction, and a sibling result stays
    committable afterwards, so committing what landed is strictly better than throwing it away. The
    failure count rides on the outcome, because a half-done pass that reported clean would be worse
    than either.

    When EVERY task fails, nothing is committed and this RAISES. The commit door refuses an empty
    result list on purpose — Lance answers one with a zero-metric success and no new version, which
    relayed verbatim is indistinguishable from a healthy table — so calling it would turn a total
    worker failure into a 400 that reads like a malformed request.

    An EMPTY PLAN is a successful no-op: the table is at target. Commit is not called, for the same
    reason as above.
    """
    planned = plan(table_id, dict(policy))
    if not planned.tasks:
        log.info("compaction_distributed_nothing_to_do", extra={"uri": uri, "table_id": table_id, "read_version": planned.read_version})
        return DistributedOutcome(read_version=planned.read_version, tasks_planned=0, tasks_executed=0)

    dataset = _open_for_rewrite(uri, write_options)
    results: list[str] = []
    failed = 0
    for index, task_json in enumerate(planned.tasks):
        try:
            results.append(_execute_one(task_json, dataset))
        except Exception as exc:  # noqa: BLE001 — one task's failure must not cost the rest their commit
            failed += 1
            log.warning(
                "compaction_task_failed",
                extra={"uri": uri, "table_id": table_id, "task": index, "of": len(planned.tasks), "error": str(exc)},
            )

    if not results:
        raise DistributedCompactionError(
            f"no task of {len(planned.tasks)} completed for {table_id}; nothing was committed. "
            "Refusing rather than falling back to the in-pod rewrite, which would restore the memory ceiling this path removes."
        )

    committed = commit(table_id, results)
    log.info(
        "compaction_distributed_committed",
        extra={
            "uri": uri,
            "table_id": table_id,
            "read_version": planned.read_version,
            "version": committed.version,
            "tasks_executed": len(results),
            "tasks_failed": failed,
            "fragments_removed": committed.fragments_removed,
        },
    )
    return DistributedOutcome(
        read_version=planned.read_version,
        tasks_planned=len(planned.tasks),
        tasks_executed=len(results),
        tasks_failed=failed,
        version=committed.version,
        fragments_added=committed.fragments_added,
        fragments_removed=committed.fragments_removed,
    )
