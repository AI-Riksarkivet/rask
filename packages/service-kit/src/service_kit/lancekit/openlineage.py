"""OpenLineage facet primitives — the spec-2-0-2 RunEvent contract, kernel-owned.

The shared half of lineage emission, used by the annotation write path
(``service_kit.lancekit.lineage_emit``): ``WriteResult``, the schema/columnLineage facets,
and the standalone ``build_run_event`` mirror. The spec constants (``SCHEMA_URL``, the facet
``_schemaURL``s, ``run_id_for``) and the columnLineage BUILDER are IMPORTED from
``service_kit.openlineage``, not re-declared — one definition, so two emitters cannot describe
the same run differently.

Kernel layer: pure over a pyarrow schema + measured stats, no pipeline import. The batch
derivers that once sat above this (a ``Stage``-aware ``measure_stage`` / ``emit_stage_lineage``
pair in the dissolved pipeline package) are gone; a workload that needs stage-level lineage
emits it from its own sealed runner.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
from pydantic import BaseModel, Field

from service_kit.lancekit.blobs import blob_field_names
from service_kit.openlineage import (
    DATASOURCE_FACET_SCHEMA_URL,
    ERROR_MESSAGE_FACET_SCHEMA_URL,
    RUN_EVENT_SCHEMA_URL,
    SCHEMA_FACET_SCHEMA_URL,
    ColumnEdge,
    column_lineage_facet,
)
from service_kit.openlineage import run_id_for as _run_id_for


# ── Spec constants — IMPORTED from service_kit/openlineage.py, not re-declared ──
# The merge landed, so "mirroring" the constants by hand is now pure drift risk: the copies here had
# already diverged (SchemaDatasetFacet pinned at 1-1-1 vs 1-2-0, DatasourceDatasetFacet at 1-0-1 vs
# 1-0-0) and every URL dropped the ``#/$defs/<Facet>`` JSON pointer the spec and the official client
# both carry. One import, one spec version, one place to bump.
SCHEMA_URL = RUN_EVENT_SCHEMA_URL
_SCHEMA_FACET_URL = SCHEMA_FACET_SCHEMA_URL
_DATASOURCE_FACET_URL = DATASOURCE_FACET_SCHEMA_URL
_ERROR_MESSAGE_FACET_URL = ERROR_MESSAGE_FACET_SCHEMA_URL
#: ``OutputStatisticsOutputDatasetFacet`` has no ``service_kit.openlineage`` constant yet — the lance-ns
#: emitters build it inline in ``medallion/schemas/events.py``. Pinned to the same published version.
_OUTPUT_STATS_FACET_URL = "https://openlineage.io/spec/facets/1-0-2/OutputStatisticsOutputDatasetFacet.json#/$defs/OutputStatisticsOutputDatasetFacet"
# The URI identifying the EMITTING CODE in every event (spec: `producer`). It named a path in the
# repo this kernel was merged FROM, inside a package that has since been dissolved — so every event
# pointed provenance-readers at code that no longer exists anywhere.
# Nothing dispatches on the string (verified: no matcher in lineage/notifications; `run_id_for`
# does not include it), so correcting it changes no behaviour, only where a reader lands.
PRODUCER = "https://github.com/AI-Riksarkivet/rask/tree/main/packages/service-kit"

#: One field→field edge as a WRITE measures it: (output_field, input_field, transformation_subtype).
#: Carried columns are "IDENTITY"; derived artifacts are "TRANSFORMATION".
#:
#: NAMED APART from ``service_kit.openlineage.ColumnEdge``, which is the SEVEN-tuple the wire builder
#: consumes. Both were called ``ColumnEdge`` in one distribution, so which shape a `list[ColumnEdge]`
#: held depended on which module the reader had open — and the two are structurally compatible enough
#: that mixing them fails at runtime, not at the import. This one is the narrow measurement shape; it
#: is WIDENED onto the wire shape at emit (see :func:`_widen`).
ColumnMapEdge = tuple[str, str, str]


class WriteResult(BaseModel):
    """The measured outcome of one write — mirrors lance-ns's WriteResult.

    Field names/shape match theirs on purpose so ``build_run_event`` (ours or theirs)
    consumes it unchanged.
    """

    version: int
    row_count: int
    size_bytes: int
    fields: list[dict[str, str]] = Field(default_factory=list)
    column_map: list[ColumnMapEdge] = Field(default_factory=list)


#: Extension names a JSON column can carry — ``pa.json_()`` (the chunk schema's ``alignments_json``,
#: the topic-tree ``hierarchy``) reads back as ``arrow.json``; Lance's docs name it ``lance.json``.
#: pyarrow 24 has no ``pa.types.is_json``, so match the extension name. MUST match
#: ``service_kit/lakehouse/schema.py`` — the same column has to carry the same label on both sides of the merge.
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
    Mirrors service_kit.lakehouse.schema.facet_fields (blob → "blob")."""
    blobs = set(blob_field_names(schema))
    return [{"name": f.name, "type": "blob" if f.name in blobs else _type_label(f.type)} for f in schema]


#: Deterministic spec-valid UUID runId for a seed (e.g. ``"<stage>-<token>"``) — re-exported from
#: ``service_kit.openlineage`` rather than recomputing the uuid5 namespace, so the id a pre-merge run mints
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
        # `column_lineage_facet` returns {} when no edge is well-formed, and an EMPTY facet must not
        # be attached: the consumer would materialise a junk `(:Column {field:""})` from it.
        column_lineage = column_lineage_facet(PRODUCER, _widen(result.column_map, inputs))
        if column_lineage:
            output_facets["columnLineage"] = column_lineage
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


def _widen(edges: list[ColumnMapEdge], inputs: list[tuple[str, str]]) -> list[ColumnEdge]:
    """Widen the write's narrow edges onto the wire shape the SHARED builder consumes.

    THERE IS ONE columnLineage BUILDER, and this module no longer carries a second. Its private copy
    grouped the same edges into the same ``fields[out].inputFields[].transformations[]`` shape as
    ``service_kit.openlineage.column_lineage_facet`` — with two differences that were both bugs, not
    choices: it emitted a facet for edges with an empty output or input field (a junk
    ``(:Column {field:""})`` on the consumer, which the shared builder exists to refuse), and it
    pinned ``_schemaURL`` from its own alias, so a spec bump applied to one emitter and not the other.

    A write measures ``(out_field, in_field, subtype)``; the wire wants
    ``(out_field, in_namespace, in_name, in_field, type, subtype, masking)``. The input dataset is the
    run's FIRST input — a write has exactly one — and the transformation type is ``DIRECT`` because
    every edge a write measures is a field read straight from that input; the subtype carries whether
    it was carried (``IDENTITY``) or derived.
    """
    in_ns, in_name = inputs[0] if inputs else ("", "")
    return [(out_field, in_ns, in_name, in_field, "DIRECT", subtype, False) for out_field, in_field, subtype in edges]
