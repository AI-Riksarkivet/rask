"""Lineage emission — START first, terminal always, including FAIL.

The medallion emitted only on COMPLETE (`iiif_produce.py` contains zero `event_type="FAIL"`), so a
harvest that raised became a 400 with NO lineage record: a failed run was indistinguishable from one
that never happened. Both halves are fixed here — a run is in the graph before it does work, and it
leaves a terminal record whichever way it ends.

`LineageRecorder` records in-process; the lineage-kit `ClientEmitter` transport lands with the
lineage wiring step. The seam is what matters now: the workflow calls `start()`/`terminal()` and does
not know which is behind it.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)


class LineageEvent(BaseModel):
    run_id: str
    event_type: str
    project: str = ""
    dataset: str = ""
    source_kind: str = ""
    version: int | None = None
    rows: int = 0
    errors: dict[str, str] = Field(default_factory=dict)


# Module-level, not a class attribute: `lineage_emitter()` builds a fresh recorder per activity, so
# a per-instance list would drop every event and a mutable class attribute is the same shared state
# wearing a disguise. Replaced by lineage-kit's emitter at the lineage-wiring step; until then this
# is the seam, and being explicit about the sharing is the point.
_EVENTS: list[LineageEvent] = []


def recorded_events() -> list[LineageEvent]:
    """The events emitted so far — the read side tests and the run-status join use."""
    return list(_EVENTS)


def reset_events() -> None:
    _EVENTS.clear()


class LineageRecorder:
    """Collects events. Deliberately never raises — lineage is OBSERVATIONAL (I8).

    A failed emission must not fail the run: lineage records what happened, it does not drive data
    flow. That is the whole point of I8, and the reason the cascade triggers on the catalog's
    publication event rather than on a lineage event as the medallion's `/bronze-arrival` did.
    """

    def start(self, run_id: str, project: str, dataset: str, kind: str, options: dict[str, Any]) -> None:
        self._record(LineageEvent(run_id=run_id, event_type="START", project=project, dataset=dataset, source_kind=kind))

    def terminal(self, run_id: str, status: str, version: int | None, rows: int, errors: dict[str, str]) -> None:
        event_type = "FAIL" if status == "FAILED" else "COMPLETE"
        self._record(LineageEvent(run_id=run_id, event_type=event_type, version=version, rows=rows, errors=errors))

    def _record(self, event: LineageEvent) -> None:
        try:
            _EVENTS.append(event)
            logger.info("lineage %s run=%s", event.event_type, event.run_id)
        except Exception:
            logger.warning("lineage emission failed for run=%s", event.run_id, exc_info=True)
