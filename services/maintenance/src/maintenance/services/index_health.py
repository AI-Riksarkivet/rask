"""Which of a dataset's indices an optimize is due on: rows it does not cover, or too many delta segments.

Read-only. Maintenance builds and optimizes no index (ADR 0156 C6); it names the ones a bring-your-own job should
optimize, on the table's due record (:mod:`service_kit.lakehouse.maintenance_due`).

MEASURED against pylance 9.0.0. ``stats.index_stats(name)`` returns ``num_indexed_rows``, ``num_unindexed_rows``,
``num_indexed_fragments``, ``num_unindexed_fragments`` and ``num_indices`` (the delta segments a query fans out over);
``describe_indices()`` returns ``IndexDescription`` OBJECTS — not dicts — with ``name``, ``index_type`` and
``field_names``.
"""

from __future__ import annotations

import logging
from typing import Any

from service_kit.lakehouse.maintenance_due import IndexDue


log = logging.getLogger(__name__)

#: Delta segments tolerated before an optimize is due. One is the steady state after an optimize that appended rather
#: than merged; a handful is where query fan-out starts to show.
DEFAULT_MAX_DELTAS = 4


def _stats(ds: Any, name: str) -> dict[str, Any] | None:  # noqa: ANN401 — LanceDataset, no protocol
    """The index's statistics, or ``None`` when they cannot be read.

    `BaseException`, deliberately. `index_stats` PANICS on index types Lance has no stats implementation for, and a
    pyo3 panic surfaces as `pyo3_runtime.PanicException`, which derives from BaseException. Measured on the live estate
    2026-08-16: the panic escaped a narrower handler and answered the whole sweep with HTTP 500. The class cannot be
    caught by name because pyo3 synthesises `pyo3_runtime` lazily and it is not importable.
    """
    try:
        stats = ds.stats.index_stats(name)
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        log.warning("index_stats_unreadable", extra={"index": name, "error": str(exc), "error_type": type(exc).__name__})
        return None
    return stats if isinstance(stats, dict) else None


def _readable_stats(ds: Any, index: Any) -> tuple[str, dict[str, Any]] | None:  # noqa: ANN401 — LanceDataset / IndexDescription
    """A user index's name and statistics, or ``None`` for a system index or one whose health is unknown.

    A JSON index is never asked: `index_stats` on one hits a `todo!()` in Lance (`lance-index/src/scalar/json.rs:95`),
    and the panic writes a Rust backtrace to the pod's stderr on every tick. An index whose health is unknown is not
    due: there is nothing an optimize is known to fix.
    """
    name = str(getattr(index, "name", "") or "")
    if not name or name.startswith("__"):
        return None
    if str(getattr(index, "index_type", "") or "").strip().lower() == "json":
        return None
    stats = _stats(ds, name)
    return None if stats is None else (name, stats)


def due_indices(ds: Any, *, max_deltas: int = DEFAULT_MAX_DELTAS) -> list[IndexDue]:  # noqa: ANN401 — LanceDataset, no protocol
    """Every user index an optimize is due on: one with unindexed rows, or with more than ``max_deltas`` deltas."""
    try:
        described = list(ds.describe_indices())
    except Exception as exc:
        log.warning("describe_indices_failed", extra={"error": str(exc)})
        return []
    due: list[IndexDue] = []
    for index in described:
        readable = _readable_stats(ds, index)
        if readable is None:
            continue
        name, stats = readable
        unindexed_rows = int(stats.get("num_unindexed_rows") or 0)
        deltas = int(stats.get("num_indices") or 0)
        if unindexed_rows > 0 or deltas > max_deltas:
            due.append(IndexDue(name=name, unindexed_rows=unindexed_rows, deltas=deltas))
    return due
