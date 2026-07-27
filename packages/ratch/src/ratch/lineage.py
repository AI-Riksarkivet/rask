"""OpenLineage emission for ratch stages — DRAFT (spec 2-0-2), opt-in and inert.

Status: a first cut, NOT wired into the driver yet. It builds the pieces a stage
run must contribute to lance-ns's lineage graph, mirroring their emit contract so
it drops in at merge:

    lance-ns mover (services/medallion): transform_stage → WriteResult
        (version, row_count, size_bytes, fields, column_map) → build_run_event → publish

Here we produce the SAME `WriteResult` from a stage we wrote, plus the
`column_map` (field→field edges) DECLARED from our `Stage` model — the input the
harness turns into the `columnLineage` facet.

The facet primitives (`WriteResult`, `facet_fields`, `build_run_event`, the spec
constants) are KERNEL-owned (`common.lancekit.openlineage`, re-exported here for
the pipeline's callers); this module adds only the `Stage`-aware layer.

Two modes, one seam (`emit_stage_lineage`):
  * MERGED  — pass lance-ns's own `build_run_event` (import
    `medallion.schemas.events.build_run_event`) as `builder=`; we supply the
    measured `WriteResult` + `column_map`, they own the wire event. This is the
    intended end-state — do NOT reimplement their emitter in production.
  * STANDALONE — `builder=None` uses the kernel's `build_run_event`, a minimal
    spec-2-0-2 mirror, so a pre-merge Ray/CLI run can emit real events to a
    `LineageSink` (file/stdout/HTTP).

Nothing here imports a model client or Ray; it's pure over a written dataset path
+ the `Stage` declaration, so it's callable from the driver, a test, or a backfill.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

import lance
from common.lancekit.openlineage import (
    PRODUCER,
    SCHEMA_URL,
    ColumnEdge,
    WriteResult,
    build_run_event,
    facet_fields,
    run_id_for,
)

from ratch.core.registry import Stage, StageShape


if TYPE_CHECKING:
    from collections.abc import Callable

# Re-export the kernel primitives so existing `from ratch.lineage import …` call
# sites (driver, tests, backfills) keep working through the pipeline's own module.
__all__ = [
    "PRODUCER",
    "SCHEMA_URL",
    "ColumnEdge",
    "LineageSink",
    "WriteResult",
    "build_run_event",
    "column_map",
    "emit_stage_lineage",
    "facet_fields",
    "measure_stage",
    "run_id_for",
]


def column_map(stage: Stage) -> list[ColumnEdge]:
    """The DECLARED field→field edges of a stage, from its `Stage` model alone.

    This is the "declared" column lineage (the in-process path in lance-ns). The
    distributed path RECONSTRUCTS the same edges from the two on-disk schemas; the
    two must agree, so this stays a pure function of the declaration:

    * carried key/read columns → (col, col, "IDENTITY")
    * each output column       → (out, primary_input, "TRANSFORMATION")

    where primary_input is the blob column (BLOB/APPEND over a blob) or the first
    read column (SCAN). A minted key not present upstream (e.g. `frame_idx`) is
    TRANSFORMATION off the primary input.
    """
    primary_input = stage.blob_column or (stage.read_columns[0] if stage.read_columns else "")
    carried = set(stage.read_columns)
    edges: list[ColumnEdge] = []
    # Identity: every read column carried through (for APPEND_ROWS these are the
    # parent keys that identify the child rows).
    for col in stage.read_columns:
        edges.append((col, col, "IDENTITY"))
    # Transformation: each declared output column, plus any key column that is
    # minted by the stage (present in key_columns but not carried from upstream).
    derived = list(stage.output_columns)
    if stage.shape is StageShape.APPEND_ROWS:
        derived += [k for k in stage.key_columns if k not in carried]
    for out in derived:
        if primary_input:
            edges.append((out, primary_input, "TRANSFORMATION"))
    return edges


def measure_stage(uri: str, storage_options: dict[str, str] | None = None) -> WriteResult:
    """Read a just-written stage's version + exact output stats + schema fields.

    Mirrors lance-ns's `measure`: version, `count_rows`, summed on-disk bytes, and
    the blob/vector-aware schema-facet fields. `column_map` is left empty here —
    attach it from `column_map(stage)` (declared) or reconstruct from schemas.
    """
    ds = lance.dataset(uri, storage_options=storage_options)
    # lance annotates DataStatistics.fields as a single FieldStatistics but returns a
    # list at runtime (upstream stub bug) — cast to the real shape, as lance-ns does.
    field_stats = cast("list[Any]", ds.stats.data_stats().fields)
    size_bytes = sum(getattr(s, "bytes_on_disk", 0) for s in field_stats)
    return WriteResult(
        version=int(ds.version),
        row_count=ds.count_rows(),
        size_bytes=size_bytes,
        fields=facet_fields(ds.schema),
    )


class LineageSink(Protocol):
    """Where a built RunEvent goes. Standalone: a file/stdout/HTTP sink. Merged: the
    Dapr outbox publish is lance-ns's job — pass their `build_run_event` and publish
    downstream instead of using this."""

    def emit(self, event: dict[str, Any]) -> None: ...


def emit_stage_lineage(
    *,
    stage: Stage,
    output_uri: str,
    output_namespace: str,
    output_name: str,
    input_datasets: list[tuple[str, str]],
    event_time: str,
    sink: LineageSink,
    storage_options: dict[str, str] | None = None,
    builder: Callable[..., dict[str, Any]] | None = None,
    operation: str = "TRANSFORM",
    job_namespace: str = "ratch",
) -> dict[str, Any]:
    """Measure a written stage, attach its declared column lineage, build the
    RunEvent, and hand it to `sink`. Returns the event (also for tests).

    `builder=None` uses the kernel's standalone mirror; pass lance-ns's
    `build_run_event` when merged. The one call the driver would add after a
    successful stage write.
    """
    result = measure_stage(output_uri, storage_options)
    result.column_map = column_map(stage)
    make = builder or build_run_event
    event = make(
        operation=operation,
        job_namespace=job_namespace,
        job_name=f"{job_namespace}.{stage.name}",
        inputs=input_datasets,
        output_namespace=output_namespace,
        output_name=output_name,
        event_time=event_time,
        result=result,
        source_uri=output_uri,
        seed=f"{stage.name}-{output_name}-{result.version}",
    )
    sink.emit(event)
    return event
