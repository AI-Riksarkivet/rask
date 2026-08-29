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
from collections import deque
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


#: How many events the in-process mirror keeps. BOUNDED, and that bound is the point.
#:
#: This is module-level state in HANDLER scope (DWF-ACT-007): `lineage_emitter()` builds a fresh
#: recorder per activity, so a per-instance list would drop every event and a mutable class attribute
#: would be the same shared state wearing a disguise — but a plain list appended to by every activity
#: of every run and cleared by nothing outside the test suite is a leak for the lifetime of the pod.
#: An ingest pod serves runs indefinitely and emits two events per run plus one per terminal, so the
#: list grew without any ceiling at all.
#:
#: A ring buffer is the right shape because of what this is FOR: the in-process MIRROR of what was
#: emitted, a test convenience and a debugging window on the most recent activity — never the source
#: of truth for run status, which asks the graph itself (`runs.py` joins against the lineage service).
#: Nothing reads more than the tail, so dropping the head costs nothing and bounds the memory exactly.
_EVENT_HISTORY = 256

_EVENTS: deque[LineageEvent] = deque(maxlen=_EVENT_HISTORY)


def recorded_events() -> list[LineageEvent]:
    """The most recent `_EVENT_HISTORY` events emitted in THIS process, oldest first."""
    return list(_EVENTS)


def reset_events() -> None:
    _EVENTS.clear()


def _output_datasets(project: str, dataset: str, version: int | None, rows: int) -> list[Any]:
    """The table this run wrote, STAMPED with what it wrote — the `version` and `outputStatistics` facets.

    **THE NAME IS THE CASCADE'S TRIGGER, and it was addressing the wrong level of the hierarchy.**
    This composed `{project}${dataset}` in namespace `{project}` — for project `bind86`, the pair
    (`bind86`, `bind86$e2ewin`). The medallion's head matches on
    `project_namespace(project, bronze_namespace)` (`ingest_trigger.py:52-57`), i.e. the pair
    (`bind86-bronze`, `bind86-bronze$e2ewin`). Those never intersect, so **no ingest write could ever
    fire the cascade** — and nothing failed: the data landed, lineage recorded it, A8 passed, and
    silver was simply never woken. R23's shape exactly, since the tier is announced by the record of
    what happened rather than by a separate event.

    The level confusion is the root of it. A PROJECT selects the storage root, through the warehouse
    registry (`produce.py:60-66`); the NAMESPACE is the medallion TIER; the TABLE is the dataset. The
    live FGA store agrees — `project:bind86 -> warehouse:bind86-wh -> namespace:alpha|beta`, with
    `namespace:bronze parent table:bronze$pages` already present — so `namespace:bind86` was never a
    thing that existed. A grant against it would have turned the run green while writing somewhere
    nothing watches.

    `project_namespace` is imported rather than re-derived: a convention two services must agree on
    cannot live inside one of them, which is why it moved to service-kit.

    The FACETS are A6, and their absence was a quiet hole rather than a visible one. `terminal()` has
    always been PASSED the committed version and the row count — it recorded them on its own in-memory
    `LineageEvent` and then emitted an output carrying neither, so the graph could say a run wrote
    `bronze$pages` and could not say WHICH VERSION or HOW MUCH. That is the difference between a
    provenance record and a note that something happened: asked "where did version 7 come from", the
    graph could only answer with every run that ever touched the table.

    Both are stamped whenever they exist, on FAIL as well as COMPLETE. A failed run that committed
    fragments before dying still moved the dataset forward, and the version it left behind is exactly
    what an operator reconstructing the damage needs. `version=None` means nothing was committed, and
    then no version facet is written at all — a facet claiming version 0 would be a fact we do not have.
    """
    from lineage_kit.schemas import DatasetFacets, DatasetVersionFacet, OutputDataset, OutputDatasetFacets, OutputStatisticsFacet

    from ingest.naming import bronze_namespace_for, bronze_table_id

    if not dataset:
        return []

    facets = DatasetFacets(version=DatasetVersionFacet(datasetVersion=str(version))) if version is not None else DatasetFacets()
    return [
        OutputDataset(
            namespace=bronze_namespace_for(project),
            name=bronze_table_id(project, dataset),
            facets=facets,
            outputFacets=OutputDatasetFacets(outputStatistics=OutputStatisticsFacet(rowCount=rows)),
        )
    ]


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


def _run(
    run_id: str,
    project: str = "",
    originator: str = "",
    published: bool | None = None,
    publish_reason: str | None = None,
    publish_error: str | None = None,
) -> Any:  # noqa: ANN401 — LineageRun, imported lazily to keep this module light
    """Reconstruct this ingest run's graph run. Same inputs -> same run, in any activity.

    `namespace` is keyword-only AND has no default — `job_run()` fills it from `LineageSettings`, and
    constructing `LineageRun` directly does not. Omitting it raises TypeError at emission time, which
    I8's never-raise guard then swallows into a log line: the run completes, its data lands, and the
    graph stays empty. A8 is what surfaces that, and it did.
    """
    from lineage_kit.config import LineageSettings
    from lineage_kit.runs import LineageRun

    return LineageRun(
        job_name=JOB_NAME,
        namespace=LineageSettings().namespace,
        run_id=lineage_run_id(run_id),
        emitter=_emitter(),
        run_facets=_tenant_facet(project, run_id, originator, published, publish_reason, publish_error),
    )


def _tenant_facet(
    project: str,
    run_id: str | None = None,
    originator: str = "",
    published: bool | None = None,
    publish_reason: str | None = None,
    publish_error: str | None = None,
) -> dict[str, Any]:
    """The `lance` run facet carrying the tenant this run's write belongs to — the THIRD half of #52.

    The cascade head derives its expected namespace from this facet (`ingest_trigger._cascade_project`
    -> `project_namespace(project, bronze_namespace)`). Emitting the qualified output name without it
    is not half a fix, it is no fix: the head would read no project, fall back to the single-tenant
    pair `bronze` / `bronze$<dataset>`, and miss the very `bind86-bronze$…` output the qualification
    just produced. The two have to move together.

    Same `is_safe_project` guard as the name composition, and for the head's own stated reason — a
    project outside the path-safe shape must never become an S3 prefix or a lineage-name qualifier.
    An unsafe value yields NO facet, which degrades to exactly the single-tenant pair the output name
    also degrades to.
    """
    from lineage_kit.consume import LANCE_RUN_FACET
    from lineage_kit.schemas import custom_facet

    from ingest.naming import tenant

    safe = tenant(project)
    fields: dict[str, Any] = {}
    if safe:
        fields["project"] = safe
    # The INGEST run id — the id /ingests/{id} answers to — stated as data because the graph's own
    # run id is run_id_for('ingest:<id>'), a one-way UUID5 nothing can map back. Without this the
    # runs board could only link with the derived id, and every link was dead (measured in a
    # browser: 'No such run' on every row). Independent of the project guard on purpose — a
    # single-tenant run still has an id worth linking.
    if run_id:
        fields["run_id"] = run_id
    # The HUMAN who asked for this harvest. This run reaches lineage over HTTP, where `enforce_author`
    # replaces the author facet with the CALLER's sub — and the caller is this SERVICE
    # (`RASK_LINEAGE_SERVICE_IDENTITY`). So without this field every ingest run was announced to an
    # inbox named `service-ingest`, and the person who started it heard nothing about their own run.
    # Read by `notifications.api.lineage_events.originator_subject`; absent is byte-identical, which is
    # the right answer for a service-token call — an invented placeholder would re-create the defect.
    if originator:
        fields["originator"] = originator
    # THE PUBLICATION VERDICT. A commit is not a publication (§ D2 D-R1), and until this landed the
    # emitted COMPLETE could not tell the two apart — a refused quality gate produced an event
    # byte-identical to a published run, so the graph asserted the opposite of what happened.
    #
    # `published` is stamped for True as well as False, on purpose: if only refusals were recorded,
    # every event predating this change would read as a success, and absence would silently mean two
    # different things. `None` stays ABSENT because it is the honest third state — no version existed
    # to gate, so no gate ran, which is not a refusal.
    #
    # `from_version`/`to_version` deliberately stay OFF the wire: they answer "what row delta did this
    # publication cover", which the run's own pull surface already serves, and the question this facet
    # exists to answer is only whether the data is READY.
    if published is not None:
        fields["published"] = published
    if publish_reason:
        fields["publish_reason"] = publish_reason
    if publish_error:
        fields["publish_error"] = publish_error
    return {LANCE_RUN_FACET: custom_facet(**fields)} if fields else {}


class LineageRecorder:
    """Emits ingest run lineage through lineage-kit. Deliberately never raises (I8)."""

    def start(self, run_id: str, project: str, dataset: str, kind: str, options: dict[str, Any], originator: str = "") -> None:
        """START, with the EXTERNAL source as the input (R23).

        The input is the external source (`s3://bucket`), never a governed tier: raw is the outside world, and
        naming bronze as its own input would make the graph claim the data came from where it landed.
        """
        self._record(LineageEvent(run_id=run_id, event_type="START", project=project, dataset=dataset, source_kind=kind))
        self._emit(lambda: _run(run_id, project, originator).start(inputs=self._inputs(kind, project, dataset, options)))

    def terminal(
        self,
        run_id: str,
        status: str,
        version: int | None,
        rows: int,
        errors: dict[str, str],
        project: str = "",
        dataset: str = "",
        originator: str = "",
        published: bool | None = None,
        publish_reason: str | None = None,
        publish_error: str | None = None,
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
        # THREE terminals, and the third is not decoration — it is a guard.
        #
        # This line was `"FAIL" if status == "FAILED" else "COMPLETE"`, so introducing TERMINATED
        # without touching it would have sent a stopped run down the `else` and emitted COMPLETE. The
        # medallion's `/bronze-arrival` head fires on exactly that — an eventType COMPLETE carrying
        # the configured output pair — so terminating a run would have STARTED the
        # bronze->silver->gold cascade over a half-finished harvest. Strictly worse than the FAIL it
        # replaced, which at least stopped there.
        #
        # ABORT is OpenLineage's own state for a cancelled run and `lineage_kit` already implements
        # it (`RunTracker.abort`: "the cancelled terminal — an operator stop, a per-chunk stop, a
        # shutdown"). Using FAIL instead would leave the GRAPH saying a person's decision was a
        # defect, which is the same conflation this change removes one layer up.
        event_type = "ABORT" if status == "TERMINATED" else ("FAIL" if status == "FAILED" else "COMPLETE")
        self._record(LineageEvent(run_id=run_id, event_type=event_type, project=project, dataset=dataset, version=version, rows=rows, errors=errors))

        outputs = _output_datasets(project, dataset, version, rows)

        def emit() -> None:
            # The verdict rides the RUN facet, never the event TYPE: a refused gate is still a
            # COMPLETE (the run did its job; the DATA was declined), and the medallion cascade head
            # keys on COMPLETE — so reporting the refusal as a FAIL would cancel bronze->silver->gold
            # for a run whose rows are committed and durable.
            run = _run(run_id, project, originator, published, publish_reason, publish_error)
            if event_type == "FAIL":
                run.fail(f"ingest run {run_id} failed: {errors or 'no detail'}", outputs=outputs)
            elif event_type == "ABORT":
                # WITH `outputs`, and the first version of this withheld them on the reasoning that a
                # terminated run's fragments are never committed so a WROTE edge would assert a write
                # that did not happen. That reasoning is wrong for this estate and was measured wrong:
                # the run landed in the graph correctly and appeared on the compute run board NOWHERE,
                # because a board that finds runs through the dataset they touched cannot see one that
                # names no dataset.
                #
                # The guard against reading a half-written dataset as a real one is the READER's
                # `event_type = 'COMPLETE'` filter — `lineage/services/repository.py` calls it
                # load-bearing for precisely this case: "FAILed runs keep WROTE edges (producers()
                # shows the attempt)". FAIL passes `outputs` one branch up for the same reason. And
                # the cascade head fires only on COMPLETE, so an ABORT edge cannot wake it.
                run.abort(f"ingest run {run_id} terminated: {errors or 'no detail'}", outputs=outputs)
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
