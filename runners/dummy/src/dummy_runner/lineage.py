"""The dummy lane's OpenLineage emission — provenance the platform can read back.

**The envelope is `lineage-kit`'s, never this module's** (LIN-001, owner ruling 2026-09-18). This
file holds only what is TRUE OF THIS LANE — which states it emits, which principals are addresses,
how a catalog identifier splits into a dataset ref — and every spec constant, the wire form and the
transport come from the shared package. The stdlib copy this replaces had already drifted two facet
revisions from the platform's (`BaseFacet` at 1-0-5 against 2-0-2, `DatasetVersionDatasetFacet` at
1-0-0 against 1-0-1), and a `_schemaURL` is what a consumer follows to VALIDATE a custom facet — so a
stale one fails at the consumer, about a producer it cannot name. `to_wire()` is produced by the
official serializer, so the wire form cannot drift from the client the package pins.

**The human rides ``lance.originator``, never ``author``.** This job authenticates to the lineage
ingest as a service, and `enforce_author` overwrites the author facet with that service's verified
sub — deliberately, since honouring a producer-supplied author would let any producer file a row in
a named person's inbox. Setting `author` would therefore be silently discarded while looking handled.

**A non-personal principal is dropped rather than carried.** A role literal (`ray`, `data_eng`) or a
wildcard is not an address: carrying it writes into an inbox actor literally named `ray`. That is the
live defect in the medallion stage runners, and this lane must not reproduce it. Dropping the field loses
nothing — the event still records the run for the graph, it simply targets nobody, which is the
honest outcome for a run no person asked for.

Emission is BEST EFFORT and never raises into the transform: provenance must not fail a run that
actually produced data. `build_emitter` degrades to a logged no-op when no endpoint is configured,
and `ClientEmitter` keeps serialisation and transport in separate `try` blocks so an unserialisable
facet is distinguishable from an outage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from lineage_kit import RunEvent, build_emitter
from lineage_kit.schemas import (
    Dataset,
    DatasetFacets,
    DatasetVersionFacet,
    ErrorMessageRunFacet,
    Job,
    JobTypeJobFacet,
    OutputDataset,
    Run,
    RunFacets,
    RunState,
    custom_facet,
)

#: The only states a person is told about. START/RUNNING notify nobody by product decision, and
#: RECONCILED is lineage's own repair marker rather than an outcome anyone chose.
TERMINAL_STATES = frozenset({"COMPLETE", "FAIL"})

#: Principals that are not an address. A role literal reaches an inbox actor named after the role;
#: a wildcard is a statement about everyone and therefore about no one.
_NOT_A_PERSON = frozenset({"", "*", "user:*", "ray", "data_eng", "analyst", "htr", "service", "system"})

#: The job facet every Ray lane stamps. `jobType` is a STANDARD facet with its own published schema,
#: so it carries that schema's URL rather than `BaseFacet` — and the model lives in `lineage_kit`,
#: where the package's conformance test holds the URL equal to the installed client's.
_JOB_TYPE_FACET = JobTypeJobFacet().model_dump(by_alias=True)


def _dataset_ref(identifier: str) -> tuple[str, str]:
    """Split a catalog identifier into the ``namespace``/``name`` pair lineage nodes are keyed by.

    ``name`` stays the FULL identifier — `silver$dummy`, never `dummy` — because delivery and render
    check `table:<name>` against the FGA object, and an unqualified name matches no tenant-qualified
    grant, which counts every recipient HIDDEN rather than denied.
    """
    return identifier.split("$", 1)[0], identifier


def build_run_event(
    *,
    event_type: str,
    run_id: str,
    to_id: str,
    from_id: str,
    rows: int = 0,
    version: int | None = None,
    originator: str = "",
    project: str = "",
    error: str | None = None,
) -> RunEvent:
    """One terminal RunEvent for a dummy-lane run.

    ``version`` belongs on COMPLETE only — a FAIL committed nothing, and a fabricated version there
    would make a failed run look like it produced data.
    """
    if event_type not in TERMINAL_STATES:
        raise ValueError(f"{event_type!r} is not terminal; this lane emits only {sorted(TERMINAL_STATES)} (a START notifies nobody)")

    lance: dict[str, Any] = {"operation": "transform", "run_id": run_id, "rows": rows}
    # Both are TARGETING hints and authorize nothing — the notifications plane re-derives every
    # recipient's visibility at delivery.
    if originator and originator not in _NOT_A_PERSON:
        lance["originator"] = originator
    if project:
        lance["project"] = project

    facets = RunFacets(lance=custom_facet(**lance))
    if error is not None:
        facets.error_message = ErrorMessageRunFacet(message=error[:1000], programmingLanguage="PYTHON")

    out_namespace, out_name = _dataset_ref(to_id)
    in_namespace, in_name = _dataset_ref(from_id)
    output = OutputDataset(namespace=out_namespace, name=out_name)
    if version is not None:
        output.facets = DatasetFacets(version=DatasetVersionFacet(datasetVersion=str(version)))

    return RunEvent(
        eventType=RunState(event_type),
        eventTime=datetime.now(UTC).isoformat(),
        run=Run(runId=run_id if _is_uuid(run_id) else str(uuid.uuid5(uuid.NAMESPACE_URL, run_id)), facets=facets),
        job=Job(namespace="ray-jobs", name=f"dummy.{to_id}", facets={"jobType": _JOB_TYPE_FACET}),
        # The DERIVED_FROM edge. Without an input, silver lands in the graph as an orphan nobody can
        # trace back to bronze — which is most of what a lineage graph is for.
        inputs=[Dataset(namespace=in_namespace, name=in_name)],
        outputs=[output],
    )


def _is_uuid(value: str) -> bool:
    """``runId`` MUST be a UUID per the spec, and the official serializer enforces it.

    The hand-rolled emitter did not, so a lane passing a readable run id got a wire event the
    platform accepted and a stricter consumer would reject. Deriving one with `uuid5` keeps the id
    STABLE for a given input — a replayed run converges on the same node instead of adding a second.
    """
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def emit(event: RunEvent) -> bool:
    """Send the event to the lineage ingest. Returns whether it landed; never raises.

    Ray pods carry no Dapr sidecar, so this is the plain HTTP ingest, and `build_emitter` resolves
    the credential the same way every other rask producer does — including the rule that ONE POD RUNS
    SEVERAL IDENTITIES, so `RASK_LINEAGE_TOKEN_<IDENTITY>` wins over the shared token when the claimed
    identity has its own. That rule was measured against the live door (a second identity presenting
    the shared token → 401, while the job wrote its rows and exited SUCCEEDED), and it lives in
    `lineage_kit.config` where every producer gets it rather than in this lane alone.
    """
    return build_emitter().emit(event)
