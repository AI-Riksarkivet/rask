"""Shared OpenLineage spec constants + helpers for every lance-ns event emitter.

Keeping these in one place is what makes our hand-built ``RunEvent``s spec-true (and therefore
reusable by a Marquez-style consumer): a valid **UUID** ``runId``, the **required** top-level
``schemaURL``, and the ``_producer`` / ``_schemaURL`` every facet — standard OR custom — must carry.

We hand-build the wire dicts rather than importing ``openlineage-python``: the client is a **dev-only**
dependency (``[dependency-groups] dev`` in ``pyproject.toml``) and is deliberately kept out of the service
images, so ``lineage.seed`` (the demo producer) is the only module that constructs events through the
official ``event_v2`` / ``facet_v2`` classes. What makes the hand-built path safe is that these constants
are asserted EQUAL to the official facet classes' ``_get_schema()`` by
``tests/unit/test_openlineage_spec_conformance.py`` — bumping ``openlineage-python`` reddens CI the moment
a facet's published version moves. That gate was missing until 2026-07-26, and three of the URLs below had
already drifted (``SchemaDatasetFacet`` 1-1-1→1-2-0, ``DatasourceDatasetFacet`` and ``ErrorMessageRunFacet``
1-0-0→1-0-1 — the 1-0-0 facets ``$ref`` the retired ``1-0-2`` core spec while our envelope declares 2-0-2).
"""
# TRANSITIONAL: ported verbatim from common.openlineage for gate 3 (R19).
# Gate 5 (R21) swaps emitters onto packages/lineage-kit; no emission-shape changes here.

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable


log = logging.getLogger(__name__)

#: The top-level ``schemaURL`` every OpenLineage ``RunEvent`` must carry (spec: ``RunEvent.schemaURL``).
RUN_EVENT_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"

#: The ``BaseFacet`` schema — the spec-legal ``_schemaURL`` for our CUSTOM run facets (``lance``,
#: ``author``) that have no dedicated published schema. Every facet, standard or custom, MUST carry
#: ``_producer`` + ``_schemaURL``; a custom facet points at ``BaseFacet``, which lives in the CORE spec
#: (not a standalone facet file) — verified against the installed ``openlineage-python``.
BASE_FACET_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/BaseFacet"

#: Standard ``SchemaDatasetFacet`` schema URL — the produced dataset's columns (name + a concise,
#: blob/vector-aware type via ``service_kit.lakehouse.schema.facet_fields``). Shared here so every emitter (catalog,
#: medallion) stamps the SAME spec version; a per-emitter copy would silently drift when one bumps it.
SCHEMA_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"

#: Standard ``DatasetVersionDatasetFacet`` schema URL — the Lance version stamped on inputs/outputs (the
#: WROTE-edge version, training pins, reconcile cross-checks). Single-homed for the same no-drift reason.
VERSION_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json#/$defs/DatasetVersionDatasetFacet"

#: Standard ``DatasourceDatasetFacet`` schema URL — the physical storage URI on outputs (what lets
#: reconcile find the on-disk dataset and back-fill lost writes).
DATASOURCE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/DatasourceDatasetFacet.json#/$defs/DatasourceDatasetFacet"

#: Standard ``ErrorMessageRunFacet`` schema URL — the FAIL emitters' error payload (medallion + compaction).
ERROR_MESSAGE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet"

#: Fixed namespace for lance-ns name-based run ids (``uuid5`` of the project URL under ``NAMESPACE_URL``
#: — a constant, so the derivation is documented but not recomputed per call).
_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Borg93/lance-ns")


def run_id_for(seed: str) -> str:
    """A spec-valid UUID ``runId`` that is STABLE for ``seed`` (e.g. ``"<operation>-<token>"``).

    OpenLineage requires ``runId`` to be a UUID (Marquez rejects anything else), but the medallion
    cascade and the reconcile back-fill need DETERMINISTIC ids so an at-least-once redelivery MERGEs
    onto the same ``(:Run)`` instead of duplicating it. ``uuid5`` gives both: the same seed always
    yields the same UUID. Keep the human-readable seed as a run facet / job name for correlation —
    never as the ``runId`` itself.
    """
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, seed))


def custom_facet(producer: str, **fields: object) -> dict[str, object]:
    """Wrap a custom run-facet payload with the required ``_producer`` + ``_schemaURL`` (``BaseFacet``).

    Standard facets (version, dataSource, outputStatistics, …) carry their own published schema URL;
    our custom facets (``lance``, ``author``) have none, so they point at ``BaseFacet`` — which is what
    keeps them spec-legal for a strict consumer.
    """
    return {"_producer": producer, "_schemaURL": BASE_FACET_SCHEMA_URL, **fields}


#: Metadata-bloat cap on the schema facet (§9 P2, 2026-07-11): a thousands-of-columns table makes
#: the FACET ITSELF large (metadata bloat, not data bloat) and pushes the whole event toward the
#: bus payload ceiling the claim-check guard enforces. 512 fields ≈ tens of KiB worst-case — far
#: under the 64 KiB publish warning for the facet's share. Consumers needing the FULL schema of a
#: wider table read it from storage (the manifest IS the schema — /schema, reconcile's
#: read_storage_schema), never from the event.
FACET_MAX_FIELDS = 512


def schema_facet(producer: str, fields: object) -> dict[str, object]:
    """The standard ``SchemaDatasetFacet`` payload for an output dataset's column schema.

    One builder for every emitter so the ``_schemaURL`` spec version can never drift between the
    catalog and the medallion compute (both stamp the per-version schema onto the WROTE edge, #24).
    Caps at ``FACET_MAX_FIELDS`` (loudly): the facet stays spec-true (a shorter ``fields`` list is
    still a valid SchemaDatasetFacet), and the full schema remains readable from storage.
    """
    items = list(fields) if isinstance(fields, (list, tuple)) else fields
    if isinstance(items, list) and len(items) > FACET_MAX_FIELDS:
        log.warning(
            "schema_facet_truncated",
            extra={"fields": len(items), "cap": FACET_MAX_FIELDS},
        )
        items = items[:FACET_MAX_FIELDS]
    return {"_producer": producer, "_schemaURL": SCHEMA_FACET_SCHEMA_URL, "fields": items}


#: Standard ``ColumnLineageDatasetFacet`` schema URL — field-to-field provenance (#24 store / #1 emit).
#: One builder so the ``_schemaURL`` version can never drift between emitters (the medallion cascade + the
#: catalog DDL path); the lineage consumer persists each input→output pair as a ``DERIVED_FROM_COLUMN`` edge.
COLUMN_LINEAGE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json#/$defs/ColumnLineageDatasetFacet"

#: One flattened input→output column dependency, as the emitters declare it:
#: ``(out_field, in_namespace, in_name, in_field, transformation_type, transformation_subtype, masking)``.
ColumnEdge = tuple[str, str, str, str, str, str, bool]


def column_lineage_facet(producer: str, edges: Iterable[ColumnEdge]) -> dict[str, object]:
    """The standard ``ColumnLineageDatasetFacet`` payload for an output dataset's field-to-field provenance.

    ``edges`` is an iterable of :data:`ColumnEdge` tuples; they are grouped by ``out_field`` into the
    spec's ``fields[out].inputFields[].transformations[]`` shape (``lineage.models.Dataset.column_edges``
    parses exactly this back out). Returns ``{}`` when there are no well-formed edges — an empty facet must
    not materialise junk ``(:Column {field:""})`` on the consumer. One builder so the ``_schemaURL``
    version stays consistent across every emitter; a per-emitter copy would silently drift (#24).
    """
    grouped: dict[str, list[dict[str, object]]] = {}
    for out_field, ns, name, in_field, ttype, subtype, masking in edges:
        # Guard the three identity keys (symmetric with the consumer's name/field/out guards): a malformed
        # edge with an empty output or source column must not create a junk vertex on ingest.
        if not out_field or not name or not in_field:
            continue
        grouped.setdefault(str(out_field), []).append(
            {
                "namespace": str(ns or ""),
                "name": str(name),
                "field": str(in_field),
                "transformations": [{"type": ttype or "DIRECT", "subtype": subtype or "", "masking": bool(masking)}],
            }
        )
    if not grouped:
        return {}
    fields = {out_field: {"inputFields": inputs} for out_field, inputs in grouped.items()}
    return {"_producer": producer, "_schemaURL": COLUMN_LINEAGE_FACET_SCHEMA_URL, "fields": fields}
