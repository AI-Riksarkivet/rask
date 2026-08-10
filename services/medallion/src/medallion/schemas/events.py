"""OpenLineage RunEvent builders for the medallion services — authored THROUGH ``lineage_kit`` (R21).

:func:`build_run_event` keeps its exact signature but is now a thin construction over
:class:`lineage_kit.schemas.RunEvent` — the one compute-lineage layer every Ray plane emits with. The
facet bags are still assembled here (the ``lance``/``author`` custom facets and the standard output
facets, with the medallion's own ``_producer``), then validated into the lineage-kit models and
serialized back. **The wire must not change** (gate 5): the serialization path
(``model_dump(by_alias=True, exclude_none=True)`` + empty-facet-bag stripping) is byte-identical to the
retired hand-built dicts — pinned by ``tests/unit/test_events_parity.py`` against a frozen copy of the
old builder, plus every existing suite unmodified. The deterministic ``runId`` now derives from
``lineage_kit.run_id_for``, whose uuid5 namespace was ALIGNED to the lance-ns URL (migration note #1) so
it equals ``service_kit.openlineage.run_id_for`` for every seed — redelivery keeps MERGEing onto the
runs already in the graph.

**One field is exempt from that freeze, and exactly one:** the ``runId`` of a PROJECT-QUALIFIED emit.
Its seed changed from ``-``-joined to NUL-joined because the ``-`` form was forgeable across tenants
(the reasoning is at the derivation site in :func:`build_run_event`). That is a MIGRATION, and it is
cheap only because of what it does not touch: project-less emits — everything single-tenant, which is
what the graph mostly holds — derive exactly as before. What it does cost: already-emitted
project-qualified runs keep their old ids and are not back-fillable, and a tenant cascade in flight
across the deploy emits its remaining events under the new id, so it reads as two runs rather than one.

The lineage service ingests these into Apache AGE, growing the
``(:Dataset)-[:DERIVED_FROM]->(:Dataset)`` edge that *is* the medallion DAG.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from lineage_kit.runs import run_id_for
from lineage_kit.schemas import Dataset, Job, OutputDataset, Run, RunEvent, RunFacets, RunState

from service_kit.lakehouse.schema import SchemaFields
from service_kit.openlineage import (
    DATASOURCE_FACET_SCHEMA_URL,
    ERROR_MESSAGE_FACET_SCHEMA_URL,
    RUN_EVENT_SCHEMA_URL,
    VERSION_FACET_SCHEMA_URL,
    column_lineage_facet,
    custom_facet,
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
    """One dataset entry's wire dict — the facet bag this module still authors (medallion ``_producer``);
    validated into :class:`lineage_kit.schemas.Dataset`/``OutputDataset`` by :func:`build_run_event`."""
    ds: dict[str, Any] = {"namespace": namespace, "name": name}
    facets: dict[str, Any] = {}
    # schema is an OUTPUT facet — the produced dataset's real columns (name + concise type, blob/vector
    # aware). Present only when the compute measured a write and captured the schema; inputs omit it.
    if schema_fields:
        facets["schema"] = schema_facet(_PRODUCER, schema_fields)
    # columnLineage is an OUTPUT facet — field-to-field provenance (#1). Present only when the transform
    # declared its input→output column edges; empty edges collapse to no facet (helper returns {}).
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
    # dataSource is an OUTPUT facet — the physical Lance URI, present only when the compute knows it.
    if source_uri:
        facets["dataSource"] = {
            "_producer": _PRODUCER,
            "_schemaURL": _DATASOURCE_FACET_SCHEMA,
            "name": source_uri,
            "uri": source_uri,
        }
    # outputStatistics is an OUTPUT facet — present only when the compute actually measured a write.
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
    """The standard ``sourceCodeLocation`` job facet — where this job's code lives (asserted, not measured)."""
    return {
        "_producer": _PRODUCER,
        "_schemaURL": _SOURCE_LOCATION_FACET_SCHEMA,
        "type": "git",
        "url": _REPO_URL,
        "path": "services/medallion",
        "branch": "main",
    }


def _wire(event: RunEvent) -> dict[str, Any]:
    """The event's wire dict, BYTE-IDENTICAL to the retired hand-built shape (parity-pinned).

    ``model_dump(by_alias=True, exclude_none=True)`` round-trips the typed facets and passes the custom
    bags through verbatim (including intentional ``null``\\ s inside them, e.g. ``outputStatistics.size``);
    the only reshaping needed is dropping the EMPTY facet bags pydantic materializes where the incumbent
    wire omitted the key entirely (an input's ``facets``, a bare FAIL output's, ``outputFacets``).
    """
    wire = event.model_dump(by_alias=True, exclude_none=True)
    for ds in [*wire.get("inputs", []), *wire.get("outputs", [])]:
        if ds.get("facets") == {}:
            del ds["facets"]
        if ds.get("outputFacets") == {}:
            del ds["outputFacets"]
    return wire


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
    event_time: str | None = None,
    synthetic: bool = False,
    models: list[str] | None = None,
    commit_sha: str | None = None,
) -> dict[str, Any]:
    """Build the OpenLineage ``RunEvent`` (wire JSON) for one medallion transform — via ``lineage_kit``.

    ``inputs`` is a list of ``(namespace, name)`` upstream datasets (external source URIs
    sources at the bronze ingest head, R23; empty for the dummy seed). The single output carries the standard version facet so the ``WROTE`` edge records the
    Lance version, plus — when the compute measured the write (``row_count`` / ``size_bytes`` set) — the
    standard ``outputStatistics`` facet, when the compute knows the URI (``source_uri``) the standard
    ``dataSource`` facet, and when the quality gate validated the write (``assertions`` set) the standard
    ``dataQualityAssertions`` facet. The ``runId`` is a DETERMINISTIC UUID derived from
    ``<operation>-<token>`` — project-qualified to ``<project>\\x00<operation>\\x00<token>`` when
    ``project`` is set, so two tenants reusing the same token never collide onto one run (spec-valid +
    stable across redelivery either way). The separator is NUL rather than ``-`` because both
    caller-supplied fields admit ``-``, which made the joined seed forgeable; see the derivation site.
    The raw ``token`` rides the ``lance`` run facet for cascade correlation.
    ``project`` (#84 per-tenant routing) also rides the ``lance`` facet, so the cascade head
    (``/bronze-arrival``) can copy it onto the stage trigger — omitted when unset, keeping the
    single-tenant emit (run id included) byte-identical. ``event_type='FAIL'`` + ``error_message``
    records a failed run (no version, no outputs asserted); the standard ``errorMessage`` run facet
    carries the reason.

    ``event_time`` pins the ``eventTime`` a caller has already fixed for the run instead of stamping
    ``now()``. The silver→gold promotion needs this: the gold ``lineage`` JSONB column (R26) is written
    INSIDE the dataset and must name the same instant as the event published to the graph, or the two
    provenance records would differ on the one field a consumer joins runs by time on. Absent → ``now()``,
    byte-identical to before.
    """
    lance_fields: dict[str, Any] = {"operation": operation}
    if not synthetic:
        # Dropped for SYNTHETIC only — deliberately NOT for FAIL, whose wire is frozen byte-for-byte by
        # `tests/unit/test_events_parity.py` against the legacy builder. Gating on `describes_no_data`
        # here reads more symmetrical and breaks that contract; a FAIL run's run-facet version is
        # pre-existing behaviour and not this change's to alter.
        #
        # It must be dropped for synthetic, though, or the phantom is merely MOVED: stripping the
        # output dataset's version while leaving `{"operation": …, "version": 1, "synthetic": true}` on
        # the run facet still asserts a version that was never written, to any consumer reading the run
        # rather than the output.
        lance_fields["version"] = version
    if token:
        lance_fields["token"] = token
    if project:
        lance_fields["project"] = project
    if synthetic:
        # Machine-readable, so a consumer filters provenance-only runs DELIBERATELY rather than by
        # inferring it from a missing version facet (which is also what a merely old event looks like).
        lance_fields["synthetic"] = True
    run_facets: dict[str, Any] = {"lance": custom_facet(_PRODUCER, **lance_fields)}
    if models:
        # #88 step 6 — model identity on the RUN, parsed from the run's own artefact (the ALTO),
        # never from a mover's config: a config states a request, the artefact states what loaded.
        # Absent models render NO facet (the FAIL/synthetic byte-parity below stays intact), and an
        # absent sha is omitted rather than nulled — the serializers' silence-is-honest rule.
        model_fields: dict[str, Any] = {"models": models}
        if commit_sha:
            model_fields["commit"] = commit_sha
        run_facets["model"] = custom_facet(_PRODUCER, **model_fields)
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
    # Deterministic UUID keyed on (project+)operation+token → stable across redelivery (idempotent MERGE)
    # yet distinct across tenants. lineage_kit.run_id_for == service_kit.openlineage.run_id_for (the
    # aligned namespace, note #1), so every id already in the graph keeps MERGEing. A token-less call
    # (defensive fallback — the cascade always threads one) gets a fresh random UUID.
    #
    # `\x00` and not `-`, because the separator has to be a character NO field can contain. Both
    # caller-supplied fields admit `-`: PROJECT_PATTERN (`service_kit.lakehouse.warehouse_registry`)
    # is `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` and SAFE_TOKEN_PATTERN (`medallion.services.trigger_guards`)
    # is `[A-Za-z0-9._-]{1,64}`, so a `-` join is FORGEABLE out of the fields it separates — and this
    # id is the MERGE key for the (:Run) node in AGE, so a forged one lands another tenant's run on
    # yours. It is reachable with a single `operation` (which is per-mover env config, not caller
    # input): ("acme", "embed_features", "evil-embed_features-tok1") and
    # ("acme-embed_features-evil", "embed_features", "tok1") both rendered
    # `acme-embed_features-evil-embed_features-tok1`. Same fix, same reason as `ingest.runs.run_id_for`
    # and `flows.routes.run_id_for`, each of which pins the old form as actually colliding.
    #
    # The project-LESS branch is left EXACTLY as it was, deliberately: it is the pre-#84 seed every
    # single-tenant run already in the graph derives from, and with `operation` fixed per deployment it
    # carries no caller-controlled second field to forge with. STATED SCOPE-CUT — that branch stays
    # non-injective in (operation, token), reachable only by an operator setting MEDALLION_OPERATION to
    # a prefix of another mover's operation+token. Closing it too would re-derive every single-tenant
    # id in the graph, which is a migration and not a bug fix. The two families cannot collide with
    # each other: a NUL-bearing seed is unreachable from the `-`-joined one.
    if token:
        run_id = run_id_for("\x00".join((project, operation, token)) if project else f"{operation}-{token}")
    else:
        run_id = str(uuid.uuid4())
    # A FAIL run produced no data: it keeps a BARE output (name only) and NO version/stats/assertions.
    failed = event_type.upper() in {"FAIL", "ABORT"}
    # A SYNTHETIC run produced no data either, for a different reason (compute is disabled — the chart's
    # default), so it describes none: same bare output. Claiming `version: 1` for a dataset that was never
    # written is indistinguishable, to every consumer, from a real v1 write.
    describes_no_data = failed or synthetic
    # Resolve the stage's declared column map to full columnLineage edges (one upstream input by design).
    column_edges: list[tuple[str, str, str, str, str, str, bool]] | None = None
    if column_map and len(inputs) == 1:
        in_ns, in_name = inputs[0]
        column_edges = [(out_field, in_ns, in_name, in_field, "DIRECT", subtype, False) for out_field, in_field, subtype in column_map]
    output = (
        _dataset(output_namespace, output_name)
        if describes_no_data
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
    )
    event = RunEvent(
        event_type=RunState(event_type.upper()),
        event_time=event_time or datetime.now(UTC).isoformat(),
        run=Run(run_id=run_id, facets=RunFacets.model_validate(run_facets)),
        job=Job(namespace=job_namespace, name=operation, facets={"sourceCodeLocation": _job_source_location()}),
        inputs=[Dataset.model_validate(_dataset(ns, name)) for ns, name in inputs],
        outputs=[OutputDataset.model_validate(output)],
        producer=_PRODUCER,
        schema_url=RUN_EVENT_SCHEMA_URL,
    )
    return _wire(event)
