"""Emit a spec-2-0-2 OpenLineage RunEvent for an annotation write (pre-merge).

At merge, lance-ns's mover emits lineage when the write routes through the catalog;
until then we emit it ourselves from the write path, using the kernel's OpenLineage
primitives (``service_kit.lancekit.openlineage``, whose spec constants match
``service_kit.openlineage``) so a pre-merge event and a merged event describe
the same run identically. Configurable sink (``MEDIA_LINEAGE_SINK=log|none``)
— no external dependency; the catalog/NATS transport is the merge step.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from service_kit.lancekit.openlineage import WriteResult, build_run_event, facet_fields


if TYPE_CHECKING:
    import lance

# `__name__`, NOT the hardcoded "lineage" this used. That name is the LINEAGE SERVICE's package tree
# — the one `obs.configure_app_logging` raises to INFO — so this emitter, which runs inside the
# annotator, borrowed another service's logger: its level was governed by a service that does not run
# in this process, and every record it wrote was attributed to a service that did not produce it.
logger = logging.getLogger(__name__)

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
    when the sink is ``none``) so callers/tests can inspect it.

    ``sink`` is a :class:`service_kit.media.config.LineageSink` value, taken as ``str`` so this
    module stays below the media settings rather than importing back up into them.
    """
    if sink == "none":
        return None
    event = build_save_event(
        ds=ds,
        table_uri=table_uri,
        table_name=table_name,
        unit_key=unit_key,
        operation=operation,
    )
    # THROUGH THE LOGGING SYSTEM, always. A `sys.stdout.write` branch used to serve
    # ``MEDIA_LINEAGE_SINK=stdout``, which put the one record describing a governed write past the
    # request-id correlation filter, past the level gate and past the OTLP export — while landing on
    # the same stream `setup_logging()` already writes to. See `LineageSink` for why the option went.
    logger.info("openlineage %s", json.dumps(event, separators=(",", ":")))
    return event
