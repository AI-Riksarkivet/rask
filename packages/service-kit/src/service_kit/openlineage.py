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

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from typing import Any, Final

from pydantic import BaseModel, ConfigDict


log = logging.getLogger(__name__)

#: The top-level ``schemaURL`` every OpenLineage ``RunEvent`` must carry (spec: ``RunEvent.schemaURL``).
RUN_EVENT_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"

#: Where rask publishes its custom facets' schemas (`spec/facets/rask/`): under a git tag, since OpenLineage asks a
#: facet's ``_schemaURL`` to be "an immutable pointer to the version of the facet schema", a tag or a sha, never a
#: branch. A changed schema is a new tag. ``lineage_kit.signing.SIGNATURE_FACET_SCHEMA_URL`` names the same tag.
RASK_FACET_SCHEMAS: Final = "https://raw.githubusercontent.com/AI-Riksarkivet/rask/facets-1.0.0/spec/facets/rask"
#: The ``_schemaURL`` of :data:`LANCE_FACET`, a JSON pointer to its definition (`RaskLanceFacet.json`).
LANCE_FACET_SCHEMA_URL: Final = f"{RASK_FACET_SCHEMAS}/RaskLanceFacet.json#/$defs/RaskLanceFacet"
#: The ``_schemaURL`` of :data:`AUTHOR_FACET` (`RaskAuthorFacet.json`).
AUTHOR_FACET_SCHEMA_URL: Final = f"{RASK_FACET_SCHEMAS}/RaskAuthorFacet.json#/$defs/RaskAuthorFacet"

#: Standard ``SchemaDatasetFacet`` schema URL — the produced dataset's columns (name + a concise,
#: blob/vector-aware type via ``service_kit.lakehouse.schema.facet_fields``). Shared here so every emitter (catalog,
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
    """A :data:`LANCE_FACET` payload carrying ``fields``, with the ``_producer`` and the ``_schemaURL`` every facet needs."""
    return {"_producer": producer, "_schemaURL": LANCE_FACET_SCHEMA_URL, **fields}


def author_facet(producer: str, subject: str) -> dict[str, object]:
    """The :data:`AUTHOR_FACET` naming ``subject``, a principal key, as both its ``sub`` and its ``name``."""
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
#: One builder so the ``_schemaURL`` version can never drift between emitters (the medallion cascade + the
#: catalog DDL path). Lineage keeps the facet inside the recorded event, its ingest authorizer and ``/events`` gate
#: on the column sources it names, and a forwarded graph backend draws its column edges from it.
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


#: rask's own dataset and run facet: the ref an input was read on or a run wrote, and the uuids the catalog minted for the
#: dataset's table and its namespace. Custom, so its key is ``{prefix}_{name}`` (OpenLineage spec, "Custom Facet Naming")
#: and its schema is rask's own (:data:`LANCE_FACET_SCHEMA_URL`, :func:`lance_facet`).
LANCE_FACET: Final = "rask_lance"
#: rask's run facet (a DatasetEvent's dataset facet) naming the verified subject a run was authored by, ``{"name", "sub"}``.
#: Custom, named as :data:`LANCE_FACET` is, with its own schema (:data:`AUTHOR_FACET_SCHEMA_URL`, :func:`author_facet`).
AUTHOR_FACET: Final = "rask_author"
#: The :data:`LANCE_FACET` key for the incarnation of the ref it names: the last uuid of pylance's ``branch_identifier`` chain,
#: on a run's facet for the ref it wrote and on an input's for the ref it was read on ([[LH-413]]).
BRANCH_IDENTIFIER: Final = "branchIdentifier"
_TABLE_UUID: Final = "tableUuid"
_NAMESPACE_UUID: Final = "namespaceUuid"


class DatasetUuids(BaseModel):
    """The uuids the catalog minted for a dataset's table and that table's namespace (ADR 0173 §10).

    A dataset's identity across a rename is its table's uuid, not its name: a name can be given up and taken by another
    table, a uuid cannot. ``None`` where the dataset has none, an external source's always, or where it was not resolved.
    """

    model_config = ConfigDict(frozen=True)

    table_uuid: str | None = None
    namespace_uuid: str | None = None


def dataset_uuids(dataset: object) -> DatasetUuids:
    """The uuids an OpenLineage dataset's ``rask_lance`` facet carries, reading only the plain ``facets`` slot.

    Tolerant of a payload that is not the shape it should be, since every reader holds an event it did not build: a value
    that is not a non-empty string is no uuid.
    """
    facets = dataset.get("facets") if isinstance(dataset, dict) else None
    lance = facets.get(LANCE_FACET) if isinstance(facets, dict) else None
    lance = lance if isinstance(lance, dict) else {}

    def text(key: str) -> str | None:
        value = lance.get(key)
        return value if isinstance(value, str) and value else None

    return DatasetUuids(table_uuid=text(_TABLE_UUID), namespace_uuid=text(_NAMESPACE_UUID))


def with_dataset_uuids(dataset: dict[str, Any], uuids: DatasetUuids, *, producer: str) -> dict[str, Any]:
    """``dataset`` with its ``rask_lance`` facet naming exactly ``uuids``: a uuid set where it has one, and removed where it has none.

    Removed rather than kept, so a stamp is the whole answer: a dataset carrying a uuid the stamping service did not
    resolve would keep a claim nobody checked. The ``rask_lance`` facet's other fields (an input's ``ref``) stay, and its
    ``_producer`` and ``_schemaURL`` become ``producer``'s and rask's schema, since the stamped facet is rask's to describe
    whatever a client's said ([[LH-500]]). The facet is left out when it would hold nothing but those two. ``dataset`` itself
    is not changed.
    """
    facets = dict(dataset.get("facets") or {})
    lance = dict(facets.get(LANCE_FACET) or {})
    for key, value in ((_TABLE_UUID, uuids.table_uuid), (_NAMESPACE_UUID, uuids.namespace_uuid)):
        if value:
            lance[key] = value
        else:
            lance.pop(key, None)
    if set(lance) - {"_producer", "_schemaURL"}:
        facets[LANCE_FACET] = {**lance, "_producer": producer, "_schemaURL": LANCE_FACET_SCHEMA_URL}
    else:
        facets.pop(LANCE_FACET, None)
    stamped = {**dataset, "facets": facets}
    if not facets:
        stamped.pop("facets")
    return stamped


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


#: The UUID namespace for a static metadata event's derived identity. A fixed literal rather than a
#: hash of anything, because it must be byte-identical in every service that derives one — a namespace
#: that varies is two namespaces.
_STATIC_EVENT_NAMESPACE: Final = uuid.UUID("6f0d9bb2-3c47-5f1a-9b8e-4a2d1c7e5f30")

#: OpenLineage's spec URL for a `DatasetEvent` — a dataset change that no job performed.
DATASET_EVENT_SCHEMA_URL: Final = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/DatasetEvent"


def static_event_id(*, producer: str, namespace: str, name: str, operation: str, event_time: str) -> str:
    """The identity of a ``DatasetEvent`` — derived from its content, never minted.

    A static metadata event carries no run id, and two things downstream need one anyway: the durable
    feed keys a static change's row on this id (``lineage.services.provenance.feed_key``), so a minted id
    would land the row again on every at-least-once redelivery; and the notifications plane keys a
    pointer by ``${run_id}@${state}``, so a minted id would put two rows in an inbox for one change.

    DERIVED HERE BECAUSE IT HAS TWO INDEPENDENT READERS SEEING DIFFERENT COPIES. The notifications bus
    lane reads the catalog's raw publish while its feed lane reads what lineage stored, and the two
    must agree exactly or one change lands twice. A shared function is the only thing that makes that
    true by construction rather than by two implementations happening to match.

    ``event_time`` IS PART OF THE IDENTITY, and both directions matter: a redelivery carries the same
    instant and so collapses onto one id, while a genuine repeat — a table dropped and created again —
    carries a different one and stays a second fact rather than being silently swallowed by the first.
    """
    return str(uuid.uuid5(_STATIC_EVENT_NAMESPACE, "|".join((producer, namespace, name, operation, event_time))))


def event_identity(event: dict[str, Any]) -> str:
    """The identity of one OpenLineage event on the wire, whichever shape it is.

    A run event IS its run id. A ``DatasetEvent`` has none, and three hops need one anyway — the
    catalog's outbox stages under it (a wrong key stages under a name the relay cannot find, which is
    the provenance loss the outbox exists to prevent), lineage's durable feed dedups on it, and the
    notifications plane keys a pointer by it. One derivation, so a single change cannot acquire three
    identities and be staged, fed and delivered as three different facts.
    """
    run = event.get("run")
    if isinstance(run, dict) and run.get("runId"):
        return str(run["runId"])
    dataset = event.get("dataset")
    dataset = dataset if isinstance(dataset, dict) else {}
    facets = dataset.get("facets")
    facets = facets if isinstance(facets, dict) else {}
    lance = facets.get(LANCE_FACET)
    lance = lance if isinstance(lance, dict) else {}
    return static_event_id(
        producer=str(event.get("producer") or ""),
        namespace=str(dataset.get("namespace") or ""),
        name=str(dataset.get("name") or ""),
        operation=str(lance.get("operation") or ""),
        event_time=str(event.get("eventTime") or ""),
    )
