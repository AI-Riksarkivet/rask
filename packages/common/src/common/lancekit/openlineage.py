"""OpenLineage facet primitives — the spec-2-0-2 RunEvent contract, kernel-owned.

The shared half of lineage emission: the pieces BOTH the annotation write path
(``common.lancekit.lineage_emit``) and the batch derivers (``ratch.lineage``)
build — ``WriteResult``, the schema/columnLineage facets, and the standalone
``build_run_event`` mirror. The spec constants (``SCHEMA_URL``, the facet
``_schemaURL``s, ``run_id_for``) are IMPORTED from ``common.openlineage``, not
re-declared — a pre-merge event and a merged event describe the same run
identically because they share one definition, not because two copies agree.

Kernel layer: pure over a pyarrow schema + measured stats, no ``Stage`` and no
pipeline import. The ``Stage``-aware measurement (``column_map``, ``measure_stage``,
``emit_stage_lineage``) lives up in ``ratch.lineage``, which imports from here.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from pydantic import BaseModel, Field

from common.lancekit.blobs import blob_field_names
from common.openlineage import (
    COLUMN_LINEAGE_FACET_SCHEMA_URL,
    DATASOURCE_FACET_SCHEMA_URL,
    ERROR_MESSAGE_FACET_SCHEMA_URL,
    RUN_EVENT_SCHEMA_URL,
    SCHEMA_FACET_SCHEMA_URL,
)
from common.openlineage import run_id_for as _run_id_for


# ── Spec constants — IMPORTED from services/common/openlineage.py, not re-declared ──
# The merge landed, so "mirroring" the constants by hand is now pure drift risk: the copies here had
# already diverged (SchemaDatasetFacet pinned at 1-1-1 vs 1-2-0, DatasourceDatasetFacet at 1-0-1 vs
# 1-0-0) and every URL dropped the ``#/$defs/<Facet>`` JSON pointer the spec and the official client
# both carry. One import, one spec version, one place to bump.
SCHEMA_URL = RUN_EVENT_SCHEMA_URL
_SCHEMA_FACET_URL = SCHEMA_FACET_SCHEMA_URL
_COLUMN_LINEAGE_FACET_URL = COLUMN_LINEAGE_FACET_SCHEMA_URL
_DATASOURCE_FACET_URL = DATASOURCE_FACET_SCHEMA_URL
_ERROR_MESSAGE_FACET_URL = ERROR_MESSAGE_FACET_SCHEMA_URL
#: ``OutputStatisticsOutputDatasetFacet`` has no ``common.openlineage`` constant yet — the lance-ns
#: emitters build it inline in ``medallion/schemas/events.py``. Pinned to the same published version.
_OUTPUT_STATS_FACET_URL = "https://openlineage.io/spec/facets/1-0-2/OutputStatisticsOutputDatasetFacet.json#/$defs/OutputStatisticsOutputDatasetFacet"
PRODUCER = "https://github.com/Borg93/lance-audio/tree/main/packages/ratch"

#: One field→field edge: (output_field, input_field, transformation_subtype).
#: Carried columns are "IDENTITY"; derived artifacts are "TRANSFORMATION".
ColumnEdge = tuple[str, str, str]


class WriteResult(BaseModel):
    """The measured outcome of one write — mirrors lance-ns's WriteResult.

    Field names/shape match theirs on purpose so ``build_run_event`` (ours or theirs)
    consumes it unchanged.
    """

    version: int
    row_count: int
    size_bytes: int
    fields: list[dict[str, str]] = Field(default_factory=list)
    column_map: list[ColumnEdge] = Field(default_factory=list)


#: Extension names a JSON column can carry — ``pa.json_()`` (the chunk schema's ``alignments_json``,
#: the topic-tree ``hierarchy``) reads back as ``arrow.json``; Lance's docs name it ``lance.json``.
#: pyarrow 24 has no ``pa.types.is_json``, so match the extension name. MUST match lance-ns
#: ``services/common/schema.py`` — the same column has to carry the same label on both sides of the merge.
_JSON_EXTENSION_NAMES = frozenset({"arrow.json", "lance.json"})


def _type_label(dtype: pa.DataType) -> str:
    """Concise lineage type label — vector/binary/json specialised, else pyarrow repr
    (blobs are labelled in ``facet_fields``, which knows the blob column names)."""
    if getattr(dtype, "extension_name", None) in _JSON_EXTENSION_NAMES:
        return "json"
    if pa.types.is_fixed_size_list(dtype) or pa.types.is_list(dtype) or pa.types.is_large_list(dtype):
        return f"array<{dtype.value_type}>"
    if pa.types.is_binary(dtype) or pa.types.is_large_binary(dtype):
        return "binary"
    return str(dtype)


def facet_fields(schema: pa.Schema) -> list[dict[str, str]]:
    """``SchemaDatasetFacet.fields`` — ``[{"name","type"}]`` per column, blob-aware.
    Mirrors lance-ns common/schema.facet_fields (blob → "blob")."""
    blobs = set(blob_field_names(schema))
    return [{"name": f.name, "type": "blob" if f.name in blobs else _type_label(f.type)} for f in schema]


#: Deterministic spec-valid UUID runId for a seed (e.g. ``"<stage>-<token>"``) — re-exported from
#: ``common.openlineage`` rather than recomputing the uuid5 namespace, so the id a pre-merge run mints
#: is byte-identical to the merged one and a redelivery MERGEs onto a single ``(:Run)``.
run_id_for = _run_id_for


def build_run_event(
    *,
    operation: str,
    job_namespace: str,
    job_name: str,
    inputs: list[tuple[str, str]],
    output_namespace: str,
    output_name: str,
    event_time: str,
    result: WriteResult,
    source_uri: str | None = None,
    event_type: str = "COMPLETE",
    error_message: str | None = None,
    seed: str | None = None,
) -> dict[str, Any]:
    """A minimal, spec-2-0-2 OpenLineage ``RunEvent`` (standalone mirror).

    Prefer lance-ns's ``medallion.schemas.events.build_run_event`` when merged — pass
    it to ``emit_stage_lineage(builder=...)``. This exists so a pre-merge run can emit
    the same-shaped event; keep it faithful, don't extend it past their facets.
    """
    run_id = run_id_for(seed or f"{operation}-{output_name}")
    output_facets: dict[str, Any] = {
        "schema": {"_producer": PRODUCER, "_schemaURL": _SCHEMA_FACET_URL, "fields": result.fields},
        "outputStatistics": {
            "_producer": PRODUCER,
            "_schemaURL": _OUTPUT_STATS_FACET_URL,
            "rowCount": result.row_count,
            "size": result.size_bytes,
        },
    }
    if result.column_map:
        output_facets["columnLineage"] = _column_lineage_facet(result.column_map, inputs)
    if source_uri is not None:
        output_facets["dataSource"] = {
            "_producer": PRODUCER,
            "_schemaURL": _DATASOURCE_FACET_URL,
            "name": output_name,
            "uri": source_uri,
        }
    run_facets: dict[str, Any] = {}
    if error_message is not None:
        run_facets["errorMessage"] = {
            "_producer": PRODUCER,
            "_schemaURL": _ERROR_MESSAGE_FACET_URL,
            "message": error_message,
            "programmingLanguage": "PYTHON",
        }
    return {
        "eventType": event_type,
        "eventTime": event_time,
        "producer": PRODUCER,
        "schemaURL": SCHEMA_URL,
        "run": {"runId": run_id, "facets": run_facets},
        "job": {"namespace": job_namespace, "name": job_name},
        "inputs": [{"namespace": ns, "name": name} for ns, name in inputs],
        "outputs": [{"namespace": output_namespace, "name": output_name, "facets": output_facets}],
    }


def _column_lineage_facet(edges: list[ColumnEdge], inputs: list[tuple[str, str]]) -> dict[str, Any]:
    """A ``ColumnLineageDatasetFacet``: per output field, its input field(s) + subtype.

    The kind rides ``inputFields[].transformations[]`` — the shape ColumnLineageDatasetFacet 1-2-0
    defines and the one every consumer reads (lance-ns's ``lineage.models.Dataset.column_edges``
    included). The first cut put ``transformationType`` / ``transformationSubtype`` on the *InputField*:
    the facet still validated (``InputField`` allows additional properties) but those keys only exist,
    deprecated, at the enclosing *Fields* level — so the ingest read no transformations at all and every
    edge landed with an empty type and subtype. Silent information loss, not a schema error.
    """
    in_ns, in_name = inputs[0] if inputs else ("", "")
    by_output: dict[str, list[dict[str, Any]]] = {}
    for out_field, in_field, subtype in edges:
        by_output.setdefault(out_field, []).append(
            {
                "namespace": in_ns,
                "name": in_name,
                "field": in_field,
                "transformations": [{"type": "DIRECT", "subtype": subtype, "masking": False}],
            }
        )
    return {
        "_producer": PRODUCER,
        "_schemaURL": _COLUMN_LINEAGE_FACET_URL,
        "fields": {out: {"inputFields": ins} for out, ins in by_output.items()},
    }
