"""This lane's OpenLineage emission — provenance the platform can read back ([[LIN-001]]).

**The envelope is `lineage-kit`'s, never this module's** (owner ruling 2026-09-18: the compute plane
emits, through `lineage-kit`). This file holds only what is TRUE OF THIS LANE — which states it emits,
how a store URI becomes a dataset ref, which principals are addresses — and every spec constant, the
wire form and the transport come from the shared package. A hand-rolled copy is not a style problem: a
`_schemaURL` is what a consumer follows to VALIDATE a custom facet, and the estate has already paid for
that twice, with `BaseFacet` at `1-0-5` against `2-0-2` and `JobTypeJobFacet` at `2-0-3` against the
client's `2-0-4` — each a stale pointer that fails at the consumer, about a producer it cannot name.

**THIS LANE'S DATASETS ARE OUTSIDE THE GOVERNED ESTATE, and that is why its refs look different from
the medallion's.** `runner.main` takes `--input`/`--output` as store URIs, not catalog table ids: it
reads an object-store prefix and writes one. `service_kit.lakehouse.naming.is_external_source_namespace`
already recognises that shape — "a store URI carrying a scheme (``s3://bucket``), which is the
convention for a namespaced external system" — so the namespace is the bucket URI and the name is the
key prefix. Minting a bare `stage$name` here instead would claim a governed table this lane never
wrote, and lineage's input check would authorize it as one.

**The human rides ``lance.originator``, never ``author``.** This job authenticates to the lineage
ingest as a service, and `enforce_author` overwrites the author facet with that service's verified sub
— deliberately, since honouring a producer-supplied author would let any producer file a row in a named
person's inbox. Setting `author` would be silently discarded while looking handled.

**A non-personal principal is dropped rather than carried.** A role literal (`ray`, `data_eng`) or a
wildcard is not an address: carrying it writes into an inbox actor literally named `ray`. Dropping the
field loses nothing — the event still records the run for the graph, it simply targets nobody, which is
the honest outcome for a run nobody asked for by name.

**Emission never fails the run.** A transcription that produced its pages and lost its provenance is
bad; one that lost its pages because provenance was unreachable is worse.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from lineage_kit import build_emitter, run_id_for
from lineage_kit.schemas import (
    Dataset,
    ErrorMessageRunFacet,
    Job,
    JobTypeJobFacet,
    OutputDataset,
    OutputDatasetFacets,
    OutputStatisticsFacet,
    Run,
    RunEvent,
    RunFacets,
    RunState,
    custom_facet,
)


#: This lane's job facet. `BATCH`/`RAY` because the pipeline is Ray Data; the JOB TYPE is the only
#: field that is this runner's own statement about itself.
_JOB_TYPE_FACET = JobTypeJobFacet(processingType="BATCH", integration="RAY", jobType="JOB").model_dump(by_alias=True)

#: Principals that are NOT an address. A role literal reaches no inbox — it creates one named after the
#: role — so it is dropped rather than carried. Mirrors `runners/dummy`'s list, which the medallion
#: stage runners' live defect is the argument for.
_NOT_A_PERSON = frozenset({"ray", "service", "system", "data_eng", "*"})


def dataset_ref(uri: str) -> tuple[str, str]:
    """``(namespace, name)`` for a store URI — the external-source shape, not a governed table id.

    ``s3://bucket/prefix`` becomes ``("s3://bucket", "prefix")``; a bare filesystem path becomes
    ``("file", <path>)``, which is the bare identifier `EXTERNAL_SOURCE_NAMESPACES` declares. An empty
    prefix names the bucket root rather than an empty dataset, because ``s3://bucket`` IS the dataset
    when the run reads the whole of it.
    """
    if uri.startswith("s3://"):
        bucket, _, prefix = uri.removeprefix("s3://").partition("/")
        return f"s3://{bucket}", prefix.strip("/") or bucket
    return "file", uri


def build_run_event(
    *,
    event_type: str,
    run_id: str,
    input_uri: str,
    output_uri: str,
    pipeline: str,
    rows: int = 0,
    originator: str = "",
    project: str = "",
    error: str | None = None,
) -> RunEvent:
    """One RunEvent for an htr-lane run.

    ``rows`` rides an `outputStatistics` facet on COMPLETE only — a FAIL wrote nothing, and a count
    there would make a failed run look like it produced data.
    """
    lance: dict[str, Any] = {"operation": "transform", "run_id": run_id, "pipeline": pipeline}
    # Both are TARGETING hints and authorize nothing — the notifications plane re-derives every
    # recipient's visibility at delivery.
    if originator and originator not in _NOT_A_PERSON:
        lance["originator"] = originator
    if project:
        lance["project"] = project

    facets = RunFacets(lance=custom_facet(**lance))
    if error is not None:
        facets.error_message = ErrorMessageRunFacet(message=error[:1000], programmingLanguage="PYTHON")

    in_namespace, in_name = dataset_ref(input_uri)
    out_namespace, out_name = dataset_ref(output_uri)
    output = OutputDataset(namespace=out_namespace, name=out_name)
    if event_type == RunState.COMPLETE:
        output.output_facets = OutputDatasetFacets(output_statistics=OutputStatisticsFacet(rowCount=rows))

    return RunEvent(
        eventType=RunState(event_type),
        eventTime=datetime.now(UTC).isoformat(),
        run=Run(runId=run_id_for(run_id), facets=facets),
        job=Job(namespace="ray-jobs", name=f"htr.{pipeline}", facets={"jobType": _JOB_TYPE_FACET}),
        # The DERIVED_FROM edge. Without an input the output lands in the graph as an orphan nobody can
        # trace back to the pages it was made from, which is most of what a lineage graph is for.
        inputs=[Dataset(namespace=in_namespace, name=in_name)],
        outputs=[output],
    )


def emit(event: RunEvent) -> bool:
    """Send the event to the lineage ingest. Returns whether it landed; never raises.

    Ray pods carry no Dapr sidecar, so this is the plain HTTP ingest, and `build_emitter` resolves the
    credential the way every rask producer does — including the rule that ONE POD RUNS SEVERAL
    IDENTITIES, so `RASK_LINEAGE_TOKEN_<IDENTITY>` wins over the shared token. That rule was measured
    against the live door (a second identity presenting the shared token -> 401, while the job wrote
    its rows and exited SUCCEEDED), and it lives in `lineage_kit.config` where every producer gets it
    rather than in this lane alone.
    """
    try:
        return build_emitter().emit(event)
    except Exception:
        return False


def originator_and_project() -> tuple[str, str]:
    """The targeting hints this lane was submitted with, from its own environment.

    Read here rather than taken as CLI flags because the SUBMITTER supplies them — the medallion's
    train and stage lanes stamp the same two on their Ray jobs — and a flag would make them a thing an
    operator types, which is a thing an operator gets wrong.
    """
    return os.environ.get("RASK_ORIGINATOR", ""), os.environ.get("RASK_PROJECT", "")
