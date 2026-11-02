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

What a READER of the events needs (rask's own facet keys and schemas, the uuids a dataset carries, an event's identity)
is the platform's, `service_kit.lineage_events`, so a client reads the lakehouse's events without this kernel; the
builders here are the lakehouse's emitters' and build on it.
"""

from __future__ import annotations

import logging
import uuid
from typing import Final

from service_kit.lineage_events import AUTHOR_FACET_SCHEMA_URL, LANCE_FACET_SCHEMA_URL


log = logging.getLogger(__name__)

#: The top-level ``schemaURL`` every OpenLineage ``RunEvent`` must carry (spec: ``RunEvent.schemaURL``).
RUN_EVENT_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"

#: Standard ``SchemaDatasetFacet`` schema URL — the produced dataset's columns (name + a concise,
#: blob/vector-aware type via ``lakehouse_kit.schema.facet_fields``). Shared here so every emitter (catalog,
#: medallion) stamps the SAME spec version; a per-emitter copy would silently drift when one bumps it.
SCHEMA_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-2-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"

#: Standard ``DatasetVersionDatasetFacet`` schema URL — the Lance version stamped on inputs/outputs (the
#: version each lineage edge records, training pins). Single-homed for the same no-drift reason.
VERSION_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/DatasetVersionDatasetFacet.json#/$defs/DatasetVersionDatasetFacet"

#: Standard ``DatasourceDatasetFacet`` schema URL — the physical storage URI on outputs (where a graph backend
#: the lineage feed is forwarded to records the dataset lives).
DATASOURCE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/DatasourceDatasetFacet.json#/$defs/DatasourceDatasetFacet"

#: Standard ``ErrorMessageRunFacet`` schema URL — the FAIL emitters' error payload (medallion + compaction).
ERROR_MESSAGE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet"

#: Fixed namespace for lance-ns name-based run ids (``uuid5`` of the project URL under ``NAMESPACE_URL``
#: — a constant, so the derivation is documented but not recomputed per call).
_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Borg93/lance-ns")


def run_id_for(seed: str) -> str:
    """A spec-valid UUID ``runId`` that is STABLE for ``seed`` (e.g. ``"<operation>-<token>"``).

    OpenLineage requires ``runId`` to be a UUID (Marquez rejects anything else), but the medallion
    cascade and maintenance's per-dataset FAIL run need DETERMINISTIC ids so an at-least-once redelivery
    lands on the same feed key instead of duplicating the run. ``uuid5`` gives both: the same seed always
    yields the same UUID. Keep the human-readable seed as a run facet / job name for correlation —
    never as the ``runId`` itself.
    """
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, seed))


def lance_facet(producer: str, **fields: object) -> dict[str, object]:
    """A ``rask_lance`` payload (`service_kit.lineage_events.LANCE_FACET`) carrying ``fields``, with the ``_producer`` and
    the ``_schemaURL`` every facet needs."""
    return {"_producer": producer, "_schemaURL": LANCE_FACET_SCHEMA_URL, **fields}


def author_facet(producer: str, subject: str) -> dict[str, object]:
    """The ``rask_author`` facet (`service_kit.lineage_events.AUTHOR_FACET`) naming ``subject``, a principal key, as both
    its ``sub`` and its ``name``."""
    return {"_producer": producer, "_schemaURL": AUTHOR_FACET_SCHEMA_URL, "name": subject, "sub": subject}


#: Metadata-bloat cap on the schema facet (§9 P2, 2026-07-11): a thousands-of-columns table makes
#: the FACET ITSELF large (metadata bloat, not data bloat) and pushes the whole event toward the
#: bus payload ceiling the claim-check guard enforces. 512 fields ≈ tens of KiB worst-case — far
#: under the 64 KiB publish warning for the facet's share. Consumers needing the FULL schema of a
#: wider table read it from storage (the manifest IS the schema), never from the event.
FACET_MAX_FIELDS = 512


def schema_facet(producer: str, fields: object) -> dict[str, object]:
    """The standard ``SchemaDatasetFacet`` payload for an output dataset's column schema.

    One builder for every emitter so the ``_schemaURL`` spec version can never drift between the
    catalog and the medallion compute (both stamp the per-version schema onto the output dataset, #24).
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


#: Standard ``LifecycleStateChangeDatasetFacet`` schema URL — what a DDL operation did to the dataset's
#: EXISTENCE or SHAPE, in the spec's own vocabulary.
#:
#: WHY THE STANDARD FACET AND NOT ONLY ``rask_lance.operation``. The estate stamps a rask-private operation
#: name (``CREATE_TABLE``, ``DROP_COLUMNS``, …) inside the custom ``rask_lance`` run facet, and no consumer
#: that is not rask can read it — so to any OpenLineage-native reader a create, a drop and an alter are
#: the same event with a different opaque string. This is the field they DO read. Both are emitted: the
#: rask name is more specific than the enum admits, and collapsing to the enum alone would lose it.
LIFECYCLE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/LifecycleStateChangeDatasetFacet.json#/$defs/LifecycleStateChangeDatasetFacet"

#: rask's DDL vocabulary mapped onto the spec's six values (ALTER/CREATE/DROP/OVERWRITE/RENAME/TRUNCATE).
#:
#: KEYED LOWERCASE, AND LOOKED UP LOWERCASE, because the estate emits both spellings and the wire form
#: is the lower one. Measured on the deployed feed 2026-09-19 over the last 500 events: `create_table`
#: 111, `drop_table` 39, `add_columns` 7, `update_schema_metadata` 6, `declare_table` 2,
#: `create_table_version` 2 — while the constants in the source read `CREATE_TABLE`, `DROP_TABLE` and
#: so on. A map built from the source literals matched ONE of them, which is how the first version of
#: this shipped a facet that never appeared on a real write.
#:
#: A DATA operation is deliberately absent rather than forced into a value. The enum has no member
#: meaning "wrote rows", so `insert`, `delete`, `merge_insert`, `update`, `compaction`, `transform`,
#: `training` and the medallion's lane verbs get nothing — mapping them to `OVERWRITE` would tell a
#: reader the table was replaced. Their row-level story is the output statistics and version facets
#: that already ride the same event. `overwrite_table` is the one write that DID replace the table's
#: contents (a create or an insert in Overwrite mode over an existing table, [[LH-242]]), so it says so.
_LIFECYCLE_BY_OPERATION: Final[dict[str, str]] = {
    "create_table": "CREATE",
    "declare_table": "CREATE",
    "register_table": "CREATE",
    "drop_table": "DROP",
    "deregister_table": "DROP",
    "add_columns": "ALTER",
    "alter_columns": "ALTER",
    "drop_columns": "ALTER",
    "create_index": "ALTER",
    "drop_index": "ALTER",
    "update_schema_metadata": "ALTER",
    "rename_table": "RENAME",
    "overwrite_table": "OVERWRITE",
}


def lifecycle_state(operation: str) -> str | None:
    """The spec lifecycle value for ``operation``, or ``None`` when it is not DDL.

    Split out of :func:`lifecycle_facet` because the same question — did this change the table's
    DEFINITION rather than its rows — also decides whether the catalog emits a ``DatasetEvent``. Two
    lists would be two places for a new operation to be forgotten, and the one that keeps emitting a
    run mints a phantom run nobody sees until they count. ``OVERWRITE`` always wrote rows and a ``CREATE`` may
    have, so the catalog keeps those writes runs (``catalog.core.lineage_emit.build_write_event``).
    """
    return _LIFECYCLE_BY_OPERATION.get(operation.lower())


def lifecycle_facet(producer: str, operation: str) -> dict[str, object]:
    """The standard ``lifecycleStateChange`` payload for ``operation``, or ``{}`` when it is not DDL.

    Returning ``{}`` rather than guessing is the point: an operation this map does not name is one the
    estate has added without deciding what it does to the dataset, and a wrong value here is worse than
    an absent one — a reader acts on ``DROP``.
    """
    state = lifecycle_state(operation)
    return {"_producer": producer, "_schemaURL": LIFECYCLE_FACET_SCHEMA_URL, "lifecycleStateChange": state} if state else {}


#: Standard ``ProcessingEngineRunFacet`` schema URL — WHICH ENGINE produced the run.
#:
#: A lakehouse that means it is multi-engine has to say which engine wrote a table, and rask already
#: has more than one write path: the catalog commits through pylance in-process, a user's pipeline
#: writes through its own engine, and a query engine is a stated direction. Without this facet every one of
#: them is an anonymous producer, and the question "what wrote this, and can I reproduce it" has no
#: answer in lineage's feed or the graph backend it is forwarded to. ``version`` is the only REQUIRED field in the spec.
PROCESSING_ENGINE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-1-1/ProcessingEngineRunFacet.json#/$defs/ProcessingEngineRunFacet"


def processing_engine_facet(producer: str, *, name: str, version: str) -> dict[str, object]:
    """The standard ``processing_engine`` run facet naming the engine and its version."""
    return {"_producer": producer, "_schemaURL": PROCESSING_ENGINE_FACET_SCHEMA_URL, "name": name, "version": version}


#: Standard ``ColumnLineageDatasetFacet`` schema URL — field-to-field provenance (#24 store / #1 emit).
#: Standard ``CatalogDatasetFacet`` schema URL — WHICH CATALOG governs this dataset.
#:
#: A lakehouse that publishes lineage to a shared consumer has to say whose catalog a dataset belongs
#: to; without it, two estates writing `bronze$events` are indistinguishable in one graph backend.
CATALOG_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-1-0/CatalogDatasetFacet.json#/$defs/CatalogDatasetFacet"

#: The storage framework, and it is a CONSTANT rather than a parameter because the estate's format is
#: closed: the catalog stores Lance tables and no other format, ever (a create naming another is
#: refused 400 at the door). A configurable value here would advertise a flexibility that does not
#: exist and that the architecture deliberately forgoes.
CATALOG_FRAMEWORK: Final = "lance"


def catalog_facet(producer: str, *, impl: str, name: str, warehouse_uri: str = "") -> dict[str, object]:
    """The standard ``catalog`` payload, or ``{}`` when the catalog cannot identify itself.

    ``impl`` is the Lance Namespace implementation (``dir``, ``rest``, …) — the spec's ``type``, whose
    own examples are ``jdbc``/``glue``/``polaris``, i.e. HOW the catalog is reached. ``name`` is the
    catalog's identity in a graph backend, which this estate already fixes as the lineage job namespace, so
    the facet and the events it rides on cannot disagree about who is speaking.

    **``metadataUri`` IS DELIBERATELY ABSENT, and the omission carries the architecture.** The spec's
    example is a JDBC string because Iceberg-style catalogs hold the commit pointer in a database;
    Lance puts the CAS in the object store, which is why this estate needs no relational DB at all.
    There is no metadata endpoint to name, and inventing one — the REST door, say — would describe a
    component that is not where the commits live.

    ``{}`` when either required field is empty: a facet whose `type` or `name` is blank is worse than
    no facet, because a consumer joins on those.
    """
    if not impl or not name:
        return {}
    facet: dict[str, object] = {
        "_producer": producer,
        "_schemaURL": CATALOG_FACET_SCHEMA_URL,
        "framework": CATALOG_FRAMEWORK,
        "type": impl,
        "name": name,
    }
    if warehouse_uri:
        facet["warehouseUri"] = warehouse_uri
    return facet


#: The ``rask_lance`` facet key (`service_kit.lineage_events.LANCE_FACET`) for the incarnation of the ref it names: the
#: last uuid of pylance's ``branch_identifier`` chain, on a run's facet for the ref it wrote and on an input's for the ref
#: it was read on ([[LH-413]]).
BRANCH_IDENTIFIER: Final = "branchIdentifier"


#: Standard ``DatasetTypeDatasetFacet`` schema URL — WHAT KIND of thing this dataset is.
DATASET_TYPE_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-1/DatasetTypeDatasetFacet.json#/$defs/DatasetTypeDatasetFacet"

#: The two values this estate can claim truthfully. A governed dataset is a Lance TABLE; a source
#: outside the estate is bytes at a location, which the spec spells FILE. The enum also admits VIEW,
#: TOPIC, STREAM, MODEL and JOB_OUTPUT — none of which the catalog serves, so none is emitted.
DATASET_TYPE_TABLE: Final = "TABLE"
DATASET_TYPE_FILE: Final = "FILE"


def dataset_type_facet(producer: str, *, external: bool) -> dict[str, object]:
    """The standard ``datasetType`` payload: ``TABLE`` for a governed dataset, ``FILE`` for a source.

    The discriminator is the caller's, and must be `naming.is_external_source_namespace` — the same
    one the lineage door's input check uses. Deciding "is this ours" twice, two ways, is how a source
    ends up governed by one rule and described by another.

    ``subType`` is not emitted. The spec's examples (MATERIALIZED, EXTERNAL, TEMPORARY) describe
    properties of a TABLE this estate does not have: nothing here is materialised from a query,
    nothing is temporary, and EXTERNAL is already said by `datasetType` being FILE.
    """
    return {
        "_producer": producer,
        "_schemaURL": DATASET_TYPE_FACET_SCHEMA_URL,
        "datasetType": DATASET_TYPE_FILE if external else DATASET_TYPE_TABLE,
    }


#: OpenLineage's spec URL for a `DatasetEvent` — a dataset change that no job performed.
DATASET_EVENT_SCHEMA_URL: Final = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/DatasetEvent"
