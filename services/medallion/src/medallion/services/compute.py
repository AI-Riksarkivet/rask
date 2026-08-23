"""The fake-Ray in-process Lance compute for the medallion cascade (the medallion-producer seam, #25 / P1 #6).

Default OFF (``MEDALLION_COMPUTE_ENABLED``): the movers/producer stay dummy-emitters (lineage, no data).
When on, each stage does a **real** Lance write — the producer seeds ``bronze$events`` (the first governed
tier, R23); each mover reads its upstream Lance dataset, applies a stage transform, and writes the
downstream one — so the emitted lineage carries the **real** Lance version and the whole event-driven loop
produces actual versioned data, not just provenance.

This is the **same** ``read → transform → write → version`` contract a distributed Ray Data job
(``medallion-producer`` on rask's KubeRay) fills in production; here it runs **in-process** so the cascade is
end-to-end testable without a Ray cluster. The compute operates on LANCE TYPES only: every stage carries
rows forward — tabular columns as tabular, vectors as vectors, blob columns of any media kind
re-materialised safely — and stamps a ``stage`` provenance column; what a stage derives from blob
payloads is dispatched on CONTENT by :mod:`medallion.services.derivers` (image → thumbnail+embedding;
unrecognised → untouched; tabular → no-op), so the same deployed mover binary serves every lane with
zero media config. Heavier per-stage ML (real encoders, captioning) is the distributed job's job at
rask. Blocking Lance/S3 IO; callers run it in the threadpool.
"""

from __future__ import annotations

from typing import Any, cast

import lance
import pyarrow as pa
from lance import blob_array, blob_field
from lance.indices.builder import IndexConfig
from lineage_kit.consume import LineageDoc, LineageEdge, as_json_rows
from pydantic import BaseModel, Field

from medallion.services.derivers import ARTIFACT_COLUMNS, derive_artifacts
from service_kit.lakehouse import blobs, schema


_STAGE_COLUMN = "stage"
#: The consume-layer provenance column (R26, executing R25b): a ``pa.json_()`` (JSONB) cell per row
#: carrying the :class:`~lineage_kit.consume.LineageDoc` of the run that wrote the row — run id, job,
#: author, operation, event time, the upstream datasets with their versions + URIs, and the
#: ``DERIVED_FROM`` chain back to bronze. Written in the SAME commit as the data, never bolted on after,
#: so a reader can never see a governed row without its provenance. Every mover stage stamps it (not
#: gold alone): silver's copy is what lets gold's chain reach bronze with no graph query — each stage
#: prepends its own hop to the chain it read off its upstream's cell.
_LINEAGE_COLUMN = "lineage"
#: The JSONB path the promotion indexes — ``run_id`` is the join key back to the lineage graph, so a
#: consumer filtering ``json_get_string(lineage, 'run_id') = …`` gets an index, not a full scan.
_LINEAGE_INDEX_PATH = "run_id"
_LINEAGE_INDEX_NAME = "lineage_run_id_idx"
#: Columns a stage RE-STAMPS rather than carries forward: ``stage`` names THIS tier and ``lineage``
#: describes THIS run, so inheriting either would label the output with its parent's provenance.
_RESTAMPED_COLUMNS = frozenset({_STAGE_COLUMN, _LINEAGE_COLUMN})
#: Row-level provenance: the stable ``_rowid`` of the BRONZE row this output descends from (bronze is the
#: root of the governed cascade, R23 — raw is the external world and owns no rows). Minted at the first
#: derive from the bronze row's reserved ``_rowid`` metacolumn (durable because every stage writes
#: ``enable_stable_row_ids=True``) and carried forward unchanged thereafter — so a gold row names the exact
#: bronze row it came from in ONE join, not a hop-by-hop walk. ``_rowid`` advances on overwrite, so this is a
#: snapshot taken at cascade-run time; a fresh cascade run over a re-ingested bronze table re-captures it.
_SOURCE_ROWID_COLUMN = "source_rowid"


class WriteResult(BaseModel):
    """The measured outcome of one fake-Ray Lance write — the new version + observed output statistics.

    ``row_count`` / ``size_bytes`` are read straight off the just-written dataset (exact, not estimated),
    so the emitted OpenLineage ``outputStatistics`` facet carries what the job *actually* produced — the
    runtime-measured numbers that move our lineage from producer-declared toward Marquez-grade. Because the
    cascade writes with ``mode="overwrite"``, the whole dataset IS this run's output, so its on-disk size
    is the size this run wrote.
    """

    version: int
    row_count: int
    size_bytes: int
    #: ``SchemaDatasetFacet`` fields (``[{"name", "type"}]``, blob/vector-aware) of the written dataset —
    #: what the emit records on the WROTE edge so the lineage graph shows real media column types.
    fields: list[dict[str, str]] = Field(default_factory=list)
    #: The stage's declared input→output column edges as ``(out_field, in_field, transformation_subtype)``
    #: — carried columns are ``IDENTITY``, derived artifacts ``TRANSFORMATION``. The emit attaches these as
    #: the standard ``columnLineage`` facet so the LIVE cascade populates the field-to-field graph (#1), not
    #: just ``seed.py``. Populated by :func:`transform_stage` (in-process, from the table it just built) and
    #: by :func:`measure_stage` (distributed, RECONSTRUCTED from the on-disk schemas of a write this process
    #: never saw). Empty only where there is genuinely nothing to declare: the bronze ingest head (no governed upstream), the
    #: dummy compute-off emit, and a bare :func:`measure` — which is why a stage the Ray job wrote MUST be
    #: read back with ``measure_stage``, or its columnLineage facet silently disappears.
    column_map: list[tuple[str, str, str]] = Field(default_factory=list)
    #: Model identities (``repo@revision``) parsed FROM THE RUN'S OWN ARTEFACT (#88 step 6 — the
    #: a transform reads them out of its own output, never from config). Empty for stages that load no
    #: model; the emit renders a ``model`` run facet only when non-empty.
    models: list[str] = Field(default_factory=list)
    #: The runner build's commit, from the same artefact. None when the document carries none.
    commit_sha: str | None = None


class UpstreamFacts(BaseModel):
    """What a stage needs to know about its upstream BEFORE it writes: where it is, which version it is
    reading, and the ``DERIVED_FROM`` chain that upstream already carries in its own ``lineage`` cell.

    The chain is inherited, not queried: the consume-layer document must be complete back to bronze
    without a round-trip to the lineage service (R25b), and reading it off the parent dataset means the
    JSONB can never contradict the graph — every hop in it was recorded by the run that emitted it.
    """

    uri: str
    version: int
    chain: list[LineageEdge] = Field(default_factory=list)
    #: The upstream's Arrow schema, carried so the stage can ask the catalog to mint its output table
    #: without a second open. Only used on a lane's FIRST run — after that the table exists and the
    #: catalog just states where — and the stage's own `overwrite` replaces it either way.
    schema: Any = None


def read_upstream(from_uri: str, storage_options: dict[str, str]) -> UpstreamFacts:
    """Open the upstream dataset and read the facts the promotion's ``lineage`` document needs.

    Blocking Lance/S3 IO (callers use the threadpool). Cheap: the version is metadata and the chain is a
    single-row read of one column — the payload is never touched.
    """
    ds = lance.dataset(from_uri, storage_options=storage_options)
    chain: list[LineageEdge] = []
    if _LINEAGE_COLUMN in ds.schema.names and ds.count_rows():
        cell = ds.to_table(columns=[_LINEAGE_COLUMN], limit=1).column(_LINEAGE_COLUMN)[0].as_py()
        chain = LineageDoc.inherited_chain(cell)
    return UpstreamFacts(uri=from_uri, version=int(ds.version), chain=chain, schema=ds.schema)


def measure(uri: str, storage_options: dict[str, str]) -> WriteResult:
    """Read the just-written dataset's version + exact output statistics (rows + on-disk bytes) + schema."""
    ds = lance.dataset(uri, storage_options=storage_options)
    # lance annotates ``DataStatistics.fields`` as a single ``FieldStatistics`` but returns a list at
    # runtime (upstream stub bug), so cast to the real shape before summing the per-field on-disk bytes.
    field_stats = cast("list[Any]", ds.stats.data_stats().fields)
    size_bytes = sum(stat.bytes_on_disk for stat in field_stats)
    return WriteResult(
        version=int(ds.version),
        row_count=ds.count_rows(),
        size_bytes=size_bytes,
        fields=schema.facet_fields(ds.schema),
    )


def measure_stage(from_uri: str, to_uri: str, storage_options: dict[str, str]) -> WriteResult:
    """Measure a stage ANOTHER engine wrote (the Ray job) and reconstruct its input→output column edges.

    The distributed path writes the downstream dataset out-of-process (``scripts/ray_stage_job.py``), so
    nothing here ever sees the transformed table — a bare :func:`measure` would return an empty
    ``column_map`` and the emit would drop the ``columnLineage`` facet, leaving the field-to-field graph (#1)
    dead exactly where production runs. The Ray job writes the SAME columns as :func:`transform_stage`
    (upstream columns carried forward + the ``stage`` stamp + whatever the blob content derived), so those
    edges are recoverable from the two ON-DISK schemas alone: an output column that already exists upstream
    is IDENTITY, an artifact column that does not is TRANSFORMATION from the blob column the deriver
    dispatches on. Schema-only — no payload is re-read.

    The Ray job writes the ``lineage`` JSONB column itself (the mover hands it the document as
    ``LINEAGE_JSON``), so provenance lands in the job's own commit exactly as in-process; what does NOT
    survive its ``mode="overwrite"`` is the JSON scalar index, which is (re)built here — the one step that
    must happen after the distributed write and can only be done by whoever measures it.
    """
    upstream_schema = lance.dataset(from_uri, storage_options=storage_options).schema
    if _LINEAGE_COLUMN in lance.dataset(to_uri, storage_options=storage_options).schema.names:
        _index_lineage(to_uri, storage_options)
    result = measure(to_uri, storage_options)
    # result.fields IS the written schema (facet_fields of the just-measured dataset) — its names are all
    # the edge reconstruction needs on the output side, so the target is opened once, not twice.
    written_columns = [field["name"] for field in result.fields]
    result.column_map = _column_map(upstream_schema, written_columns, set(blobs.blob_field_names(upstream_schema)))
    return result


#: The schema-metadata key that DECLARES a dataset's canonical lineage/FGA name.
#:
#: The maintenance sweep cannot derive it. Its URI is composed from the NAMESPACE alone
#: (`.../medallion/<namespace>`) while the canonical id is a separate literal, so `medallion/bronze` is
#: both `bronze$events` and `bronze$pages` — one path, two objects. And the name must equal the OpenFGA
#: object id, because notification delivery re-checks `can_get_metadata` against `table:<output name>`;
#: a wrong name marks every recipient HIDDEN, which is worse than emitting nothing. So the writer, which
#: is the only party that knows both, declares it.
#:
#: Read by `maintenance.core.lineage_emit.declared_table_id`. Without it the sweep emits no maintenance
#: provenance for these datasets AND no per-dataset FAIL event — which is the estate's only per-dataset
#: maintenance failure surface.
LINEAGE_DATASET_ID_KEY = "lineage.dataset_id"


def _with_declared_id(table: pa.Table, dataset_id: str | None) -> pa.Table:
    """Stamp the canonical name onto the table's schema metadata, preserving what is already there.

    MERGES rather than replaces: Lance keeps other producers' schema metadata (the #21 self-describing
    coordinates among them), and a replace would silently destroy it.
    """
    if not dataset_id:
        return table
    existing = dict(table.schema.metadata or {})
    existing[LINEAGE_DATASET_ID_KEY.encode()] = dataset_id.encode()
    return table.replace_schema_metadata(existing)


def seed_bronze(uri: str, storage_options: dict[str, str], *, rows: int = 8, dataset_id: str | None = None) -> WriteResult:
    """Seed a small synthetic ``bronze$events`` dataset — the fake medallion-producer ingest at the head of the
    cascade (R23: the producer writes the first governed tier directly; there is no raw dataset).

    Carries the ``stage`` stamp the retired raw→bronze mover used to apply (merged into the bronze ingest
    head). Overwrites any existing dataset (idempotent re-seed) and returns the resulting Lance version +
    the measured output statistics (rows + on-disk bytes) the emit records as an ``outputStatistics`` facet.
    """
    table = pa.table(
        {
            "id": pa.array(list(range(rows)), pa.int64()),
            "payload": pa.array([f"event-{i}" for i in range(rows)]),
            _STAGE_COLUMN: pa.array(["bronze"] * rows, pa.string()),
        }
    )
    # data_storage_version="2.2" — the current Lance format (blob v2 + Map need it; pylance 8 still
    # defaults to 2.1). Overwrite-mode upgrades a pre-existing 2.1 dataset forward on the next run.
    # enable_stable_row_ids — row _rowid stays constant across compaction (which rewrites fragments and
    # invalidates row ADDRESSES). This is a CREATE-TIME-ONLY flag: it cannot be turned on later, so we set it
    # at the cascade head to keep durable row identity available (e.g. to key blob carry-forward by _rowid if
    # a stage ever gains append/upsert). Free on top of overwrite; the positional read path is unaffected.
    lance.write_dataset(
        _with_declared_id(table, dataset_id),
        uri,
        mode="overwrite",
        storage_options=storage_options,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return measure(uri, storage_options)


def _lineage_column(doc: LineageDoc, rows: int) -> pa.Array:
    """The constant ``lineage`` column: ``rows`` copies of ``doc`` as Lance-stored JSONB.

    ``pa.json_()`` is the Arrow JSON extension type Lance persists as JSONB — which is what makes the
    cell queryable in place (``json_get_string`` / ``json_extract`` / ``json_exists`` /
    ``json_array_contains`` in a FILTER) instead of an opaque string a consumer has to parse row by row.
    """
    return pa.array(as_json_rows(doc, rows), pa.json_())


def _index_lineage(uri: str, storage_options: dict[str, str]) -> None:
    """Build the JSON scalar index over ``lineage -> run_id`` (R26's "indexable" half).

    A Lance JSON index is a scalar index on ONE JSONB path: ``IndexConfig(index_type="json")`` with
    ``target_index_type`` naming the underlying index and ``path`` the key. It must be (re)built after
    every stage write because the cascade writes ``mode="overwrite"``, which drops the dataset's indices.
    """
    ds = lance.dataset(uri, storage_options=storage_options)
    ds.create_scalar_index(
        _LINEAGE_COLUMN,
        IndexConfig(index_type="json", parameters={"target_index_type": "btree", "path": _LINEAGE_INDEX_PATH}),
        name=_LINEAGE_INDEX_NAME,
    )


def transform_stage(
    from_uri: str, to_uri: str, storage_options: dict[str, str], *, stage: str, lineage: LineageDoc | None = None, dataset_id: str | None = None
) -> WriteResult:
    """Read the upstream Lance dataset, transform, write the downstream dataset (the generic stage).

    Every stage stamps the ``stage`` provenance column (set, not appended twice, so re-running over an
    already-stamped upstream replaces the value), threads the row-level ``source_rowid`` provenance column
    (minted at the first derive from the bronze ``_rowid``, carried forward thereafter — so a gold row names
    the exact bronze row it descends from), carries blob columns of ANY media kind through intact
    (``_carry_forward``), and derives whatever the blob CONTENT supports (``derive_artifacts`` — image →
    thumbnail+embedding, unrecognised → untouched, tabular → no-op). Returns the new downstream Lance
    version + the measured output statistics (rows + on-disk bytes) for the emit.

    ``lineage`` (R26) stamps the consume-layer ``lineage`` JSONB column and builds its JSON scalar index.
    It is a column of the table this call writes, so the provenance lands in the SAME Lance commit as the
    data it describes; the index is a second commit (indices do not survive an ``overwrite``), which is why
    the version this returns — the one the emit records — is read AFTER both.

    SINGLE-BASE BY DESIGN (P2.1, docs/DECISIONS.md #p21--single-base-cascade-write): the cascade writes
    ``mode="overwrite"`` to ONE root per stage — it does NOT distribute a stage table across #3-B multi-base
    ``data_bases``. That is a
    deliberate boundary, not an omission: multi-base registers its bases at CREATE time only
    (``initial_bases``), the cascade is overwrite-only, and the medallion already distributes physically at
    the per-ZONE bucket level. #3-B stays REST-create-only (an explicit client signal) until a gold/training
    table demonstrably needs per-table fan-out AND the real Ray distributed-write path lands — see
    docs/DECISIONS.md #p21--single-base-cascade-write.
    """
    ds = lance.dataset(from_uri, storage_options=storage_options)
    out, blob_payloads = _carry_forward(ds, stage)
    out = derive_artifacts(out, blob_payloads)
    if lineage is not None:
        # In the SAME commit as the data (R26): a governed row must never be readable without its
        # provenance, so the JSONB is a column of the table being written, not an add_columns after it.
        out = out.append_column(pa.field(_LINEAGE_COLUMN, pa.json_()), _lineage_column(lineage, out.num_rows))
    # 2.2 + stable row ids like seed_bronze: every dataset the cascade writes is on the current format (so a blob
    # column never trips "Blob v2 requires file version >= 2.2" mid-cascade) and keeps durable row identity.
    lance.write_dataset(
        _with_declared_id(out, dataset_id),
        to_uri,
        mode="overwrite",
        storage_options=storage_options,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    if lineage is not None:
        _index_lineage(to_uri, storage_options)
    result = measure(to_uri, storage_options)
    # Declare the input→output column edges for the columnLineage facet (#1) — blob_payloads' keys ARE this
    # stage's blob columns (the deriver source). The mover attaches the single upstream dataset identity.
    result.column_map = _column_map(ds.schema, out.column_names, set(blob_payloads))
    return result


def _column_map(in_schema: pa.Schema, out_names: list[str], blob_cols: set[str]) -> list[tuple[str, str, str]]:
    """This stage's input→output column edges: ``(out_field, in_field, transformation_subtype)``.

    The generic transform carries every upstream column forward (``IDENTITY``, keyed on the same name) and
    derives blob artifacts (``thumbnail``/``embedding``) from their source blob column
    (``TRANSFORMATION``). The ``stage`` and ``lineage`` stamps are constants of THIS run with no input
    column, so they get no edge (an inherited ``lineage`` would otherwise read as IDENTITY from the
    parent's provenance, which is exactly the claim the re-stamp exists to avoid).
    A carried-forward artifact (a later stage that didn't re-derive) is IDENTITY like any other column.

    Keyed on NAMES only — the upstream schema plus the names of the written columns — so the same rules
    classify a table this process built (:func:`transform_stage`) and one only its on-disk schema is known
    for (:func:`measure_stage`, the Ray path).
    """
    in_names = {f.name for f in in_schema}
    deps: list[tuple[str, str, str]] = [(name, name, "IDENTITY") for name in out_names if name not in _RESTAMPED_COLUMNS and name in in_names]
    # source_rowid is minted at the first derive from the bronze row's reserved ``_rowid`` metacolumn (root
    # provenance) — declare that as its input edge. Once it exists it is carried forward like any column, so
    # a later stage (source_rowid in BOTH schemas) is already handled as IDENTITY by the rule above.
    if _SOURCE_ROWID_COLUMN in set(out_names) and _SOURCE_ROWID_COLUMN not in in_names:
        deps.append((_SOURCE_ROWID_COLUMN, "_rowid", "IDENTITY"))
    if blob_cols:
        source = min(blob_cols)  # matches derivers' ``min(blob_payloads)`` dispatch — deterministic source
        deps += [(artifact, source, "TRANSFORMATION") for artifact in ARTIFACT_COLUMNS if artifact in out_names and artifact not in in_names]
    return deps


def _carry_forward(ds: lance.LanceDataset, stage: str) -> tuple[pa.Table, dict[str, list[bytes | None]]]:
    """Read the upstream table and stamp the ``stage`` column, carrying any blob-v2 column through intact.

    A plain ``to_table()`` demotes a blob column to its descriptions struct (tagged with the legacy
    ``lance-encoding:blob`` key), which the 2.2 write then rejects — so blob columns are re-materialised
    as bytes and re-wrapped with ``blob_array``. A stage with no blob column keeps the cheap
    straight-through path. Returns the stamped table AND the materialised blob payloads per column
    (``None`` where the upstream payload is null), so a media stage can derive artifacts without a second
    blob pass.

    NULL-SAFE BY CONSTRUCTION (R27): the read is ONE ``blobs.read_aligned_table`` scan
    (``blob_handling="all_binary"``), not ``to_table()`` + a positional ``read_blobs``. ``read_blobs``
    DROPS null rows (measured, pylance 9.0.0 — docs/architecture/lance-blob-v2-findings.md), so the old
    two-scan shape hard-failed the whole stage on a single un-harvested page with an opaque
    ``ArrowInvalid: … expected length 3 but got length 2`` — routed as a TRANSIENT error into a RETRY
    storm and the DLQ. A null payload now carries forward AS null and the cascade proceeds.
    """
    blob_cols = blobs.blob_field_names(ds.schema)
    if not blob_cols:
        return _stamp_stage(_carry_source_rowid(_drop_inherited_lineage(ds.to_table(with_row_id=True))), stage), {}

    # ONE aligned scan for tabular AND blob columns — cardinality-preserving (nulls arrive as None) and
    # half the IO of the old scan-plus-read_blobs pair. Full-materialises payloads into memory, which is
    # fine for this in-process fake-Ray stand-in over the cascade's small overwrite-written datasets; a
    # distributed job streams instead. with_row_id so the first derive off bronze can mint source_rowid
    # from the SAME scan the rows come from (a carried source_rowid is a plain column already in this read).
    aligned = blobs.read_aligned_table(
        ds,
        columns=[f.name for f in ds.schema if f.name not in _RESTAMPED_COLUMNS],
        with_row_id=True,
    )
    rows = aligned.num_rows
    columns: dict[str, Any] = {}
    fields: list[pa.Field] = []
    blob_payloads: dict[str, list[bytes | None]] = {}
    for f in ds.schema:
        if f.name in _RESTAMPED_COLUMNS:
            continue  # re-stamped by the caller so the value reflects THIS run, not the upstream's
        if f.name in blob_cols:
            payloads = aligned.column(f.name).to_pylist()
            blob_payloads[f.name] = payloads
            fields.append(blob_field(f.name))
            columns[f.name] = blob_array(payloads)
        else:
            fields.append(aligned.schema.field(f.name))
            columns[f.name] = aligned.column(f.name)
    # Root provenance: a carried source_rowid came through the loop above (a plain upstream column); at the
    # first derive off bronze it is minted here from the just-read _rowid (same aligned scan). _rowid is not persisted.
    if _SOURCE_ROWID_COLUMN not in columns:
        fields.append(pa.field(_SOURCE_ROWID_COLUMN, pa.uint64()))
        columns[_SOURCE_ROWID_COLUMN] = aligned.column("_rowid").cast(pa.uint64())
    fields.append(pa.field(_STAGE_COLUMN, pa.string()))
    columns[_STAGE_COLUMN] = pa.array([stage] * rows, pa.string())
    return pa.table(columns, schema=pa.schema(fields)), blob_payloads


def _drop_inherited_lineage(table: pa.Table) -> pa.Table:
    """Drop an upstream ``lineage`` cell so it cannot survive into this stage's output.

    ``stage`` is re-stamped in place by :func:`_stamp_stage` (preserving column order); ``lineage`` is
    dropped instead because it is appended fresh only when the caller supplies this run's document — a
    stage invoked without one must write NO lineage column rather than the parent's.
    """
    return table.drop_columns([_LINEAGE_COLUMN]) if _LINEAGE_COLUMN in table.column_names else table


def _carry_source_rowid(table: pa.Table) -> pa.Table:
    """Root provenance — delegated to the shared stamp so the Ray driver cannot disagree with this one.

    `source_rowid` names the BRONZE row an output descends from (R23). An upstream that already carries
    it keeps it; re-minting from the immediate parent would reroot the chain one tier down.
    """
    from service_kit.lakehouse.stage_stamp import carry_source_rowid

    return carry_source_rowid(table)


def _stamp_stage(table: pa.Table, stage: str) -> pa.Table:
    """Set (or append) the ``stage`` provenance column — delegated to the shared stamp.

    IN PLACE when the column exists, and that is the half which had drifted from the Ray driver:
    dropping and re-appending moves the column to the end, and `write_dataset(mode="overwrite")` takes
    the table's schema as the dataset's — so the same lane written two ways left unequal schemas.
    """
    from service_kit.lakehouse.stage_stamp import stamp_stage

    return stamp_stage(table, stage=stage)
