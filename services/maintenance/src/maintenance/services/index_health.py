"""#60 — what ``optimize_indices()`` CANNOT fix, reported instead of assumed away.

The sweep records ``indices_optimized``, which counts the call rather than its effect. Four states
survive that call untouched, and every one of them degrades queries to a flat scan while the tick
logs success:

===========================  ==========================================================
rows left unindexed          the optimize ran and did not fold everything in
delta proliferation          each optimize APPENDS a delta; queries fan out across all
params / type drift          an index is EXTENDED under its original params, never rebuilt
a dropped index              an optimize over a dataset with no index is a green no-op
===========================  ==========================================================

WHY REPORT RATHER THAN REPAIR. Three of the four are only fixable by REBUILDING the index, which is a
full pass over the column — an FTS rebuild on a large tier is not a cost a cron tick that was asked to
compact should decide to pay. The estate's own rule for this shape is already written down: the drift
reconciler reports and deletes nothing until a human acts on the report. This follows it.

MEASURED against pylance 9.0.0 before any of this was written. ``stats.index_stats(name)`` returns
``num_indexed_rows``, ``num_unindexed_rows``, ``num_indexed_fragments``, ``num_unindexed_fragments``,
``num_indices`` and per-delta ``indices[]`` (carrying ``params`` for an inverted index);
``describe_indices()`` returns ``IndexDescription`` OBJECTS — not dicts, which is the first thing that
bites — with ``name``, ``index_type``, ``field_names`` and ``details``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel


if TYPE_CHECKING:
    from collections.abc import Sequence


log = logging.getLogger(__name__)

#: Deltas tolerated before it is worth saying so. One delta is the steady state after an optimize that
#: appended rather than merged; a handful is where query fan-out starts to show. Not a hard rule — the
#: number is a reporting threshold, and reporting is all this does.
DEFAULT_MAX_DELTAS = 4


class IndexFinding(BaseModel):
    """One thing about one index that the optimize did not, or could not, put right.

    ``note`` is written for the operator who sees it in a report with no other context, so it says
    what is wrong and what it costs rather than naming a metric.
    """

    #: The index's name, or ``""`` when the finding is that no index exists.
    name: str = ""
    column: str
    index_type: str = ""
    unindexed_rows: int = 0
    unindexed_fragments: int = 0
    deltas: int = 0
    note: str


def _stats(ds: Any, name: str) -> dict[str, Any] | None:  # noqa: ANN401 — LanceDataset, no protocol
    try:
        stats = ds.stats.index_stats(name)
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception; see below
        # `BaseException`, deliberately. `index_stats` PANICS on index types Lance has no stats
        # implementation for, and a pyo3 panic surfaces as `pyo3_runtime.PanicException`, which derives
        # from BaseException — so `except Exception` does not see it. Measured on the live estate
        # 2026-08-16: `pyo3_runtime.PanicException: not yet implemented` escaped this handler AND
        # `compact_one`'s per-dataset capture, and answered the sweep with HTTP 500.
        #
        # The blast radius is why this is worth the broad catch: index health is a REPORTING step, and
        # a report that cannot be produced for ONE index must not cost every other dataset in the
        # estate its compaction and version reclamation. The class cannot be caught by name because
        # pyo3 synthesises `pyo3_runtime` lazily and it is not importable.
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        log.warning("index_stats_unreadable", extra={"index": name, "error": str(exc), "error_type": type(exc).__name__})
        return None
    return stats if isinstance(stats, dict) else None


def inspect_indices(
    ds: Any,  # noqa: ANN401 — LanceDataset, no protocol; the estate types it this way elsewhere
    *,
    expected_columns: Sequence[str] | None = None,
    max_deltas: int = DEFAULT_MAX_DELTAS,
) -> list[IndexFinding]:
    """Everything wrong with this dataset's indices that maintenance will not fix by itself.

    ``expected_columns`` is the dropped-index check and is opt-in: without a declared expectation
    there is no way to tell "this column was never indexed" from "this column lost its index", and
    reporting every unindexed column in a wide table would be noise nobody reads. A policy that names
    the columns it depends on gets a real answer; one that names none gets the other three checks.

    Returns ``[]`` for a healthy dataset. That has to hold — a report that fires on healthy data
    trains operators to ignore it, which is worse than not reporting.
    """
    findings: list[IndexFinding] = []
    try:
        described = list(ds.describe_indices())
    except Exception as exc:
        log.warning("describe_indices_failed", extra={"error": str(exc)})
        described = []

    indexed_columns: set[str] = set()
    for index in described:
        name = str(getattr(index, "name", "") or "")
        fields = list(getattr(index, "field_names", []) or [])
        column = fields[0] if fields else ""
        index_type = str(getattr(index, "index_type", "") or "")
        indexed_columns.update(fields)

        stats = _stats(ds, name)
        if stats is None:
            findings.append(
                IndexFinding(
                    name=name,
                    column=column,
                    index_type=index_type,
                    note="its statistics could not be read, so its health is UNKNOWN — not the same as healthy",
                )
            )
            continue

        unindexed_rows = int(stats.get("num_unindexed_rows") or 0)
        unindexed_fragments = int(stats.get("num_unindexed_fragments") or 0)
        deltas = int(stats.get("num_indices") or 0)

        if unindexed_rows:
            findings.append(
                IndexFinding(
                    name=name,
                    column=column,
                    index_type=index_type,
                    unindexed_rows=unindexed_rows,
                    unindexed_fragments=unindexed_fragments,
                    deltas=deltas,
                    note=(
                        f"{unindexed_rows} row(s) across {unindexed_fragments} fragment(s) are unindexed after the "
                        f"optimize — queries touching them fall back to a full scan"
                    ),
                )
            )
        if deltas > max_deltas:
            findings.append(
                IndexFinding(
                    name=name,
                    column=column,
                    index_type=index_type,
                    deltas=deltas,
                    note=(
                        f"{deltas} delta indices (> {max_deltas}) — each optimize APPENDED rather than merged, so every "
                        f"query fans out across all of them. Only a rebuild merges them; the optimize cannot"
                    ),
                )
            )

    for column in expected_columns or ():
        if column not in indexed_columns:
            findings.append(
                IndexFinding(
                    column=column,
                    note=(
                        "no index — a dropped or never-built index makes the optimize a successful no-op, so the pass "
                        "stays green while this column is served by a full scan"
                    ),
                )
            )
    return findings
