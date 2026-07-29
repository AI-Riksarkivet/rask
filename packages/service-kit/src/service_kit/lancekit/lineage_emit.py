"""Emit a spec-2-0-2 OpenLineage RunEvent for an annotation write (pre-merge).

At merge, lance-ns's mover emits lineage when the write routes through the catalog;
until then we emit it ourselves from the write path, using the kernel's OpenLineage
primitives (``service_kit.lancekit.openlineage``, whose spec constants match
``service_kit.openlineage``) so a pre-merge event and a merged event describe
the same run identically. Configurable sink (``MEDIA_LINEAGE_SINK=stdout|log|none``)
— no external dependency; the catalog/NATS transport is the merge step.
"""
# TRANSITIONAL: ported verbatim from common.lancekit.lineage_emit for gate 3 (R19).
# Gate 5 (R21) swaps emitters onto packages/lineage-kit; no emission-shape changes here.

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from service_kit.lancekit.openlineage import WriteResult, build_run_event, facet_fields


if TYPE_CHECKING:
    import lance

logger = logging.getLogger("lineage")

#: Lineage namespace for the annotation plane (media units + the annotations table).
_NS = "media"


def build_save_event(
    *,
    ds: lance.LanceDataset,
    table_uri: str,
    table_name: str,
    unit_key: str,
    operation: str = "MERGE_INSERT",
) -> dict[str, Any]:
    """A spec-2-0-2 RunEvent for one annotation save. A human save CARRIES the schema,
    so every output column maps to itself (IDENTITY) in the columnLineage facet; the
    media unit is the input, the annotations table the output."""
    schema = ds.schema
    result = WriteResult(
        version=int(ds.version),
        row_count=ds.count_rows(),
        size_bytes=0,  # exact bytes = a stats read; skipped on the hot save path
        fields=facet_fields(schema),
        column_map=[(f.name, f.name, "IDENTITY") for f in schema],
    )
    return build_run_event(
        operation=operation,
        job_namespace=_NS,
        job_name=f"annotate.{operation.lower()}",
        inputs=[(_NS, unit_key)],
        output_namespace=_NS,
        output_name=table_name,
        event_time=datetime.now(UTC).isoformat(),
        result=result,
        source_uri=table_uri,
        event_type="COMPLETE",
        seed=f"annotate-{table_name}-{unit_key}-v{result.version}",
    )


def emit_save(
    *,
    ds: lance.LanceDataset,
    table_uri: str,
    table_name: str,
    unit_key: str,
    sink: str,
    operation: str = "MERGE_INSERT",
) -> dict[str, Any] | None:
    """Build + emit the save's RunEvent to the configured sink; returns it (or None
    when the sink is ``none``) so callers/tests can inspect it."""
    if sink == "none":
        return None
    event = build_save_event(
        ds=ds,
        table_uri=table_uri,
        table_name=table_name,
        unit_key=unit_key,
        operation=operation,
    )
    line = json.dumps(event, separators=(",", ":"))
    if sink == "stdout":
        sys.stdout.write(line + "\n")
    else:
        logger.info("openlineage %s", line)
    return event
