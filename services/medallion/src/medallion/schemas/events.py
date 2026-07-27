"""OpenLineage RunEvent builders for the medallion services.

Each (dummy) Ray job emits a standard OpenLineage ``RunEvent`` describing what it did — ``inputs`` = the
upstream stage's dataset(s), ``outputs`` = the dataset it produced — which the lineage service ingests
into Apache AGE, growing the ``(:Dataset)-[:DERIVED_FROM]->(:Dataset)`` edge that *is* the medallion DAG.
Same wire format the lineage consumer already validates (``lineage.models.RunEvent``); the ``lance`` run
facet carries the operation + version, and an ``author`` facet stamps the persona.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from service_kit.lakehouse.schema import SchemaFields
from service_kit.openlineage import (
    DATASOURCE_FACET_SCHEMA_URL,
    ERROR_MESSAGE_FACET_SCHEMA_URL,
    RUN_EVENT_SCHEMA_URL,
    VERSION_FACET_SCHEMA_URL,
    column_lineage_facet,
    custom_facet,
    run_id_for,
    schema_facet,
)


#: OpenLineage ``producer`` URI — identifies the software that emitted the event.
_PRODUCER = "https://github.com/Borg93/lance-ns/tree/main/medallion"

#: OpenLineage standard ``DatasourceDatasetFacet`` schema URL — the physical Lance URI on the output,
#: so the B4 reconcile back-fill can read the on-disk version of a dataset the CASCADE wrote (without
#: it, every compute-written medallion dataset looks ``missing_on_storage`` and reconcile skips it).
_DATASOURCE_FACET_SCHEMA = DATASOURCE_FACET_SCHEMA_URL

#: The repo the medallion services live in — the ``sourceCodeLocation`` job facet's git URL.
_REPO_URL = "https://github.com/Borg93/lance-ns"
#: Standard ``SourceCodeLocationJobFacet`` schema URL → *where the job's code lives* (git repo + path).
#: A HERE-DUMMY of what rask's runner will auto-derive from its git + pipeline once lance-ray OpenLineage
#: is wired (GOAL 3-real); today the medallion emitter asserts it from the known service location.
_SOURCE_LOCATION_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-1-0/SourceCodeLocationJobFacet.json#/$defs/SourceCodeLocationJobFacet"

#: Standard ``DatasetVersionDatasetFacet`` schema URL → the lineage WROTE edge records the Lance version.
_VERSION_FACET_SCHEMA = VERSION_FACET_SCHEMA_URL
#: Standard ``OutputStatisticsOutputDatasetFacet`` schema URL → the runtime-measured rows + on-disk bytes
#: the compute actually wrote (moves our lineage from producer-declared toward Marquez-grade). Output-only.
_OUTPUT_STATS_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-0-2/OutputStatisticsOutputDatasetFacet.json#/$defs/OutputStatisticsOutputDatasetFacet"
#: Standard ``DataQualityAssertionsDatasetFacet`` schema URL → the validator's assertions on the produced
#: dataset; a ``success: false`` entry is the record of a batch the quality gate blocked from promotion.
_DATA_QUALITY_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-1-0/DataQualityAssertionsDatasetFacet.json#/$defs/DataQualityAssertionsDatasetFacet"


def _dataset(
    namespace: str,
    name: str,
    version: int | None = None,
    row_count: int | None = None,
    size_bytes: int | None = None,
    assertions: list[dict[str, Any]] | None = None,
    source_uri: str | None = None,
    schema_fields: SchemaFields | None = None,
    column_edges: list[tuple[str, str, str, str, str, str, bool]] | None = None,
) -> dict[str, Any]:
    ds: dict[str, Any] = {"namespace": namespace, "name": name}
    facets: dict[str, Any] = {}
    # schema is an OUTPUT facet — the produced dataset's real columns (name + concise type, blob/vector
    # aware). Present only when the compute measured a write and captured the schema; inputs omit it.
    # Built by the SHARED service_kit.openlineage helper — one spec version across all emitters.
    if schema_fields:
        facets["schema"] = schema_facet(_PRODUCER, schema_fields)
    # columnLineage is an OUTPUT facet — field-to-field provenance (#1). Present only when the transform
    # declared its input→output column edges; the lineage consumer persists each as a DERIVED_FROM_COLUMN
    # edge, and the frontend "columns" view renders it. Empty edges collapse to no facet (helper returns {}).
    if column_edges:
        column_facet = column_lineage_facet(_PRODUCER, column_edges)
        if column_facet:
            facets["columnLineage"] = column_facet
    if version is not None:
        facets["version"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _VERSION_FACET_SCHEMA,
            "datasetVersion": str(version),
        }
    # dataSource is an OUTPUT facet — the physical Lance URI, present only when the compute knows it (the
    # medallion stage's TO_URI). Without it the B4 reconcile can't read the on-disk version of a dataset
    # the cascade wrote → it'd look missing_on_storage and be skipped.
    if source_uri:
        facets["dataSource"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _DATASOURCE_FACET_SCHEMA,
            "name": source_uri,
            "uri": source_uri,
        }
    # outputStatistics is an OUTPUT facet — present only when the compute actually measured a write (the
    # raw producer / dummy emit omits it). Inputs never pass row_count, so they never carry it.
    if row_count is not None or size_bytes is not None:
        facets["outputStatistics"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _OUTPUT_STATS_FACET_SCHEMA,
            "rowCount": row_count,
            "size": size_bytes,
        }
    # dataQualityAssertions is an OUTPUT facet — present only when the quality gate validated the write.
    if assertions:
        facets["dataQualityAssertions"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _DATA_QUALITY_FACET_SCHEMA,
            "assertions": assertions,
        }
    if facets:
        ds["facets"] = facets
    return ds


def _job_source_location() -> dict[str, Any]:
    """The standard ``sourceCodeLocation`` job facet — where this job's code lives.

    A HERE-DUMMY (like the ``outputStatistics`` / ``dataQualityAssertions`` facets): the medallion services
    all live in this repo under ``services/medallion``, so the emitter asserts that git location directly.
    rask's runner will auto-derive the real value (its git repo + pipeline path) when lance-ray OpenLineage
    lands (GOAL 3-real) — same facet, measured instead of declared.
    """
    return {
        "_producer": _PRODUCER,
        "_schemaURL": _SOURCE_LOCATION_FACET_SCHEMA,
        "type": "git",
        "url": _REPO_URL,
        "path": "services/medallion",
        "branch": "main",
    }


def build_run_event(
    *,
    operation: str,
    author: str | None,
    job_namespace: str,
    inputs: list[tuple[str, str]],
    output_namespace: str,
    output_name: str,
    version: int = 1,
    row_count: int | None = None,
    size_bytes: int | None = None,
    assertions: list[dict[str, Any]] | None = None,
    source_uri: str | None = None,
    schema_fields: SchemaFields | None = None,
    column_map: list[tuple[str, str, str]] | None = None,
    token: str | None = None,
    project: str | None = None,
    event_type: str = "COMPLETE",
    error_message: str | None = None,
) -> dict[str, Any]:
    """Build the OpenLineage ``RunEvent`` (wire JSON) for one medallion transform.

    ``inputs`` is a list of ``(namespace, name)`` upstream datasets (empty for the raw producer, which has
    no upstream). The single output carries the standard version facet so the ``WROTE`` edge records the
    Lance version, plus — when the compute measured the write (``row_count`` / ``size_bytes`` set) — the
    standard ``outputStatistics`` facet, when the compute knows the URI (``source_uri``) the standard
    ``dataSource`` facet, and when the quality gate validated the write (``assertions`` set) the standard
    ``dataQualityAssertions`` facet. The ``runId`` is a DETERMINISTIC UUID derived from
    ``<operation>-<token>`` — project-qualified to ``<project>-<operation>-<token>`` when ``project`` is
    set, so two tenants reusing the same token never collide onto one run (spec-valid + stable across
    redelivery either way); the raw ``token`` rides the ``lance`` run facet for cascade correlation.
    ``project`` (#84 per-tenant routing) also rides the ``lance`` facet, so the cascade head
    (``/raw-arrival``) can copy it onto the stage trigger — omitted when unset, keeping the
    single-tenant emit (run id included) byte-identical. ``event_type='FAIL'`` + ``error_message``
    records a failed run (no version, no outputs asserted); the standard ``errorMessage`` run facet
    carries the reason.
    """
    lance_fields: dict[str, Any] = {"operation": operation, "version": version}
    if token:
        lance_fields["token"] = token
    if project:
        lance_fields["project"] = project
    run_facets: dict[str, Any] = {"lance": custom_facet(_PRODUCER, **lance_fields)}
    if author:
        run_facets["author"] = custom_facet(_PRODUCER, name=author, sub=author)
    if error_message:
        # Standard errorMessage run facet — records WHY a FAIL run failed (its own published schema).
        run_facets["errorMessage"] = {
            "_producer": _PRODUCER,
            "_schemaURL": ERROR_MESSAGE_FACET_SCHEMA_URL,
            "message": error_message,
            "programmingLanguage": "PYTHON",
        }
    # Deterministic UUID keyed on (project+)operation+token → stable across redelivery (idempotent
    # MERGE) yet distinct across tenants: without the project qualifier two projects reusing one token
    # would MERGE onto the SAME run. Project absent → the exact single-tenant seed (byte-identical);
    # a token-less call (defensive fallback — the cascade always threads one) gets a fresh random UUID.
    if token:
        run_id = run_id_for(f"{project}-{operation}-{token}" if project else f"{operation}-{token}")
    else:
        run_id = str(uuid.uuid4())
    # A FAIL run produced no data: it keeps a BARE output (name only — the lineage repo makes the WROTE
    # edge so producers() surfaces the attempt, but withholds the version for a non-success run) and NO
    # version/stats/assertions. It does NOT fabricate lineage: the repo also gates DERIVED_FROM on
    # is_success, so keeping the inputs records the READ, not a derivation.
    failed = event_type.upper() in {"FAIL", "ABORT"}
    # Resolve the stage's declared column map to full columnLineage edges: a medallion transform has exactly
    # ONE upstream input, so each (out_field, in_field, subtype) edge points at inputs[0] (#1). No masking in
    # the cascade compute (identity carry-forward / artifact derivation); a PII-hash stage would set it true.
    column_edges: list[tuple[str, str, str, str, str, str, bool]] | None = None
    if column_map and len(inputs) == 1:
        in_ns, in_name = inputs[0]
        column_edges = [(out_field, in_ns, in_name, in_field, "DIRECT", subtype, False) for out_field, in_field, subtype in column_map]
    outputs = [
        _dataset(output_namespace, output_name)
        if failed
        else _dataset(
            output_namespace,
            output_name,
            version=version,
            row_count=row_count,
            size_bytes=size_bytes,
            assertions=assertions,
            source_uri=source_uri,
            schema_fields=schema_fields,
            column_edges=column_edges,
        )
    ]
    return {
        "eventType": event_type,
        "eventTime": datetime.now(UTC).isoformat(),
        "producer": _PRODUCER,
        "schemaURL": RUN_EVENT_SCHEMA_URL,
        "run": {"runId": run_id, "facets": run_facets},
        "job": {
            "namespace": job_namespace,
            "name": operation,
            "facets": {"sourceCodeLocation": _job_source_location()},
        },
        "inputs": [_dataset(ns, name) for ns, name in inputs],
        "outputs": outputs,
    }
