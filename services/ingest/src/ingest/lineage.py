"""Lineage emission — START first, terminal always, including FAIL.

The medallion emitted only on COMPLETE (`iiif_produce.py` contains zero `event_type="FAIL"`), so a
harvest that raised became a 400 with NO lineage record: a failed run was indistinguishable from one
that never happened. Both halves are fixed here — a run is in the graph before it does work, and it
leaves a terminal record whichever way it ends.

**This emits through `packages/lineage-kit` for real** (wired 2026-08-03). It previously appended to
a module-level list and nothing else, behind a docstring promising the transport "lands with the
lineage wiring step" — so the graph held nothing, and the first green in-cluster run correctly
reported A8's defect: *the data landed with no provenance record*. That is the gate working as
designed. Wiring the emitter is what makes the claim stop being true; silencing the gate would not
have.

**Two activities, one run.** START and the terminal are separate Dapr activities, so they execute in
different invocations and possibly on different pods — a context manager cannot span them. The graph
run id is therefore DERIVED from the ingest run id (`run_id_for`, a uuid5), so both activities
reconstruct the same `LineageRun` without passing state between them. A per-attempt id would leave
one orphan START in the graph for every activity replay.

**Never raises** (I8). Lineage is OBSERVATIONAL: it records what happened, it does not drive data
flow. A failed emission must not fail a run that actually landed its data — which is also why the
cascade triggers on the catalog's publication event rather than on a lineage event, as the
medallion's `/bronze-arrival` did.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

#: The job name every ingest run shares. Correlation is by run id; the name groups the lane.
JOB_NAME = "ingest.run"


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
# wearing a disguise. This is the in-process MIRROR of what was emitted — a test convenience, and
# never the source of truth for run status, which asks the graph itself.
_EVENTS: list[LineageEvent] = []


def recorded_events() -> list[LineageEvent]:
    """The events emitted so far, in THIS process."""
    return list(_EVENTS)


def reset_events() -> None:
    _EVENTS.clear()


def _delimiter() -> str:
    """The catalog's table-id separator. Read from env so it cannot drift from the catalog client's."""
    return os.getenv("RASK_CATALOG_DELIMITER", "$")


def lineage_run_id(run_id: str) -> str:
    """The graph run id for an ingest run — stable, so two activities agree without sharing state."""
    from lineage_kit import run_id_for

    return run_id_for(f"ingest:{run_id}")


def _emitter() -> Any:  # noqa: ANN401 — Emitter
    """lineage-kit's configured transport. The ingest plane does NOT publish to the lineage topic.

    It briefly did, on the theory that the cascade needs the ingest run's own event to fire. Two
    things came back from running it:

    * The Dapr publish failed outright — `pubsub lineage-pubsub is not found`, because the component
      is scoped per app and `ingest` is not on its list. I8's never-raise guard swallowed that into a
      log line, and because the topic emit ran BEFORE the HTTP one, it took the GRAPH write down with
      it: the run landed its data and recorded no provenance, which A8 reported on the very next run.
      A dual-delivery path whose first leg can silently cancel the second is worse than either leg
      alone.
    * It was not needed. The CATALOG announces the write — `lance-catalog/insert.bronze$events`,
      eventType COMPLETE, outputs `[bronze$events]` — which is exactly what the medallion's
      `/bronze-arrival` filters on. Verified in-cluster: an ingest run produced
      `POST /bronze-arrival 200` on the producer and `POST /medallion-event 200` on the
      bronze-to-silver mover, with no event from this module involved at all.

    That is the right shape, not a lucky accident. The governed WRITER announces the tier, so the
    announcement carries the catalog's authority and happens exactly once per commit. An ingest-side
    publish would be a second announcement of the same write — a double-fired cascade whose two
    triggers nothing keeps in agreement.
    """
    from lineage_kit import build_emitter

    return build_emitter()


def _run(run_id: str) -> Any:  # noqa: ANN401 — LineageRun, imported lazily to keep this module light
    """Reconstruct this ingest run's graph run. Same inputs -> same run, in any activity.

    `namespace` is keyword-only AND has no default — `job_run()` fills it from `LineageSettings`, and
    constructing `LineageRun` directly does not. Omitting it raises TypeError at emission time, which
    I8's never-raise guard then swallows into a log line: the run completes, its data lands, and the
    graph stays empty. A8 is what surfaces that, and it did.
    """
    from lineage_kit.config import LineageSettings
    from lineage_kit.runs import LineageRun

    return LineageRun(job_name=JOB_NAME, namespace=LineageSettings().namespace, run_id=lineage_run_id(run_id), emitter=_emitter())


class LineageRecorder:
    """Emits ingest run lineage through lineage-kit. Deliberately never raises (I8)."""

    def start(self, run_id: str, project: str, dataset: str, kind: str, options: dict[str, Any]) -> None:
        """START, with the EXTERNAL source as the input (R23).

        The input is `iiif://…` or `s3://bucket`, never a governed tier: raw is the outside world, and
        naming bronze as its own input would make the graph claim the data came from where it landed.
        """
        self._record(LineageEvent(run_id=run_id, event_type="START", project=project, dataset=dataset, source_kind=kind))
        self._emit(lambda: _run(run_id).start(inputs=self._inputs(kind, project, dataset, options)))

    def terminal(
        self,
        run_id: str,
        status: str,
        version: int | None,
        rows: int,
        errors: dict[str, str],
        project: str = "",
        dataset: str = "",
    ) -> None:
        """COMPLETE or FAIL — and a COMPLETE must NAME WHAT IT WROTE.

        `complete()` with no outputs was the first version, and it records a run with an input and no
        WROTE edge: the graph knows the run happened and cannot say what it produced. A8 still passed,
        because A8 asks whether the run EXISTS — which is exactly how a half-recorded provenance
        survives a provenance check.

        The output also IS the cascade's trigger. The medallion's `/bronze-arrival` head fires only on
        an event whose `eventType` is COMPLETE and whose outputs contain its configured
        `{namespace, name}` pair (`ingest_trigger.py:51-58`), so an unnamed output means a bronze
        write that wakes nothing downstream. R23 is why it works this way round: nothing publishes
        `medallion.bronze` directly — the head derives it from the write's own lineage, so a tier is
        announced by the record of what happened rather than by a second, separately-maintained event.
        """
        event_type = "FAIL" if status == "FAILED" else "COMPLETE"
        self._record(LineageEvent(run_id=run_id, event_type=event_type, project=project, dataset=dataset, version=version, rows=rows, errors=errors))

        # The catalog's own identifier for the table — `bronze$pages`, not `pages`. The graph and the
        # cascade head both key on it, so composing it differently here would make the run's output
        # name a table nothing else recognises.
        outputs = [(project, f"{project}{_delimiter()}{dataset}")] if project and dataset else []

        def emit() -> None:
            run = _run(run_id)
            if event_type == "FAIL":
                run.fail(f"ingest run {run_id} failed: {errors or 'no detail'}", outputs=outputs)
            else:
                run.complete(outputs=outputs)

        self._emit(emit)

    def _inputs(self, kind: str, project: str, dataset: str, options: dict[str, Any]) -> list[tuple[str, str]]:
        """Resolve the run's external input through the source registry — never hardcoded (I1)."""
        try:
            from ingest.sources import SourceSpec, lineage_input_for

            twin = lineage_input_for(SourceSpec(kind=kind, project=project, dataset=dataset, options=options))
        except Exception:
            logger.debug("no lineage twin for source kind %r", kind, exc_info=True)
            return []
        return [(twin.namespace, twin.name)]

    def _record(self, event: LineageEvent) -> None:
        _EVENTS.append(event)
        logger.info("lineage %s run=%s", event.event_type, event.run_id)

    def _emit(self, emit: Any) -> None:  # noqa: ANN401 — a zero-arg callable
        try:
            emit()
        except Exception:
            # I8: observational. A run whose data landed must not be reported as failed because the
            # graph was unreachable — that would turn an observability outage into a data incident.
            logger.warning("lineage emission failed", exc_info=True)
