"""The fake-Ray in-process Lance compute for the medallion cascade (the medallion-producer seam, #25 / P1 #6).

Default OFF (``MEDALLION_COMPUTE_ENABLED``): the stage runners/producer stay dummy-emitters (lineage, no data).
When on, each stage does a **real** Lance write — the producer seeds ``bronze$events`` (the first governed
tier, R23); each stage runner reads its upstream Lance dataset, applies a stage transform, and writes the
downstream one — so the emitted lineage carries the **real** Lance version and the whole event-driven loop
produces actual versioned data, not just provenance.

This is the **same** ``read → transform → write → version`` contract a distributed Ray Data job
(``medallion-producer`` on rask's KubeRay) fills in production; here it runs **in-process** so the cascade is
end-to-end testable without a Ray cluster. The compute operates on LANCE TYPES only: every stage carries
rows forward — tabular columns as tabular, vectors as vectors, blob columns of any media kind
re-materialised safely — and stamps a ``stage`` provenance column; what a stage derives from blob
payloads is dispatched on CONTENT by :mod:`medallion.services.derivers` (image → thumbnail+embedding;
unrecognised → untouched; tabular → no-op), so the same deployed stage runner binary serves every lane with
zero media config. Heavier per-stage ML (real encoders, captioning) is the distributed job's job at
rask. Blocking Lance/S3 IO; callers run it in the threadpool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

import lance
import pyarrow as pa
from pydantic import BaseModel, Field

from lineage_kit.consume import LineageDoc, LineageEdge, as_json_rows
from medallion.core.config import get_settings, shared_lance_session
from medallion.services.derivers import ARTIFACT_COLUMNS, Deriver, deriver_for
from service_kit.lakehouse import auto_cleanup, blobs, schema, tier_write
from service_kit.lakehouse.commit_marker import CommitMarker

# ONE implementation, shared with the Ray driver — the same reason the stage stamp itself lives
# there. A second copy is how the two drivers came to disagree about `stage`'s column position,
# and this key decides a more expensive question: which dataset a maintenance run is filed against.
from service_kit.lakehouse.stage_stamp import ONE_TO_ONE, declare_dataset_id, ensure_declared_dataset_id


_STAGE_COLUMN = "stage"
#: The consume-layer provenance column (R26, executing R25b): a ``pa.json_()`` (JSONB) cell per row
#: carrying the :class:`~lineage_kit.consume.LineageDoc` of the run that wrote the row — run id, job,
#: author, operation, event time, the upstream datasets with their versions + URIs, and the
#: ``DERIVED_FROM`` chain back to bronze. Written in the SAME commit as the data, never bolted on after,
#: so a reader can never see a governed row without its provenance. Every stage stamps it (not
#: gold alone): silver's copy is what lets gold's chain reach bronze with no graph query — each stage
#: prepends its own hop to the chain it read off its upstream's cell.
_LINEAGE_COLUMN = "lineage"
#: Columns a stage RE-STAMPS rather than carries forward: ``stage`` names THIS tier and ``lineage``
#: describes THIS run, so inheriting either would label the output with its parent's provenance.
_RESTAMPED_COLUMNS = frozenset({_STAGE_COLUMN, _LINEAGE_COLUMN})
#: Row-level provenance: the stable ``_rowid`` of the BRONZE row this output descends from (bronze is the
#: root of the governed cascade, R23 — raw is the external world and owns no rows). Minted at the first
#: derive from the bronze row's reserved ``_rowid`` metacolumn (durable because every stage writes
#: ``enable_stable_row_ids=True``) and carried forward unchanged thereafter — so a gold row names the exact
#: bronze row it came from in ONE join, not a hop-by-hop walk. DURABLE ACROSS RE-DERIVATION since the tiers
#: became full-sync merges: a re-seeded or re-derived upstream keeps the ids its downstream references, which
#: is the whole point of paying for stable row ids at every create.
_SOURCE_ROWID_COLUMN = "source_rowid"


class WriteResult(BaseModel):
    """The measured outcome of one fake-Ray Lance write — the new version + observed output statistics.

    ``row_count`` / ``size_bytes`` are read straight off the just-written dataset (exact, not estimated),
    so the emitted OpenLineage ``outputStatistics`` facet carries what the job *actually* produced — the
    runtime-measured numbers that move our lineage from producer-declared toward Marquez-grade. A stage is a
    FULL SYNC — rows the run no longer produces are deleted — so the whole dataset IS this run's output and
    its on-disk size is the size this run wrote.
    """

    version: int
    row_count: int
    size_bytes: int
    #: Rows the DESTINATION held before this write, or ``None`` when it did not exist — the promotion
    #: band's comparison point, observed at the only moment it is unambiguous.
    #:
    #: It is recorded here rather than reconstructed from ``version - 1`` because that reconstruction
    #: is wrong for this writer and was silently wrong in production: the data lands in one commit and
    #: the lineage index in a SECOND (an overwrite dropped every index; measured on pylance 10.0.0), so the version reported is
    #: N+1 and ``version - 1`` is N — the commit that already holds the new rows. The delta computed
    #: from it is structurally zero, which is why an 8 -> 1000 row jump published without ever asking
    #: anyone (measured 2026-08-23). Version arithmetic cannot be made safe here; the count can only be
    #: taken before the write.
    previous_row_count: int | None = None
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
    #: catalog just states where — and the stage's own full-sync merge converges on it either way.
    schema: Any = None
    #: The external blob base the upstream's manifest registers, or ``None`` for a managed upstream. The
    #: stage asks the catalog to register the SAME base when it creates its output table ([[LH-209]]): a
    #: create registers no base it was not asked for, and every carried pointer is refused under none.
    external_base: str | None = None


def read_upstream(from_uri: str, storage_options: dict[str, str]) -> UpstreamFacts:
    """Open the upstream dataset and read the facts the promotion's ``lineage`` document needs.

    Blocking Lance/S3 IO (callers use the threadpool). Cheap: the version is metadata and the chain is a
    single-row read of one column — the payload is never touched.
    """
    ds = lance.dataset(from_uri, storage_options=storage_options, session=shared_lance_session())
    chain: list[LineageEdge] = []
    if _LINEAGE_COLUMN in ds.schema.names and ds.count_rows():
        cell = ds.to_table(columns=[_LINEAGE_COLUMN], limit=1).column(_LINEAGE_COLUMN)[0].as_py()
        chain = LineageDoc.inherited_chain(cell)
    return UpstreamFacts(uri=from_uri, version=int(ds.version), chain=chain, schema=ds.schema, external_base=blobs.external_base_of(ds))


def measure(uri: str, storage_options: dict[str, str], *, version: int | None = None) -> WriteResult:
    """Read the just-written dataset's version + exact output statistics (rows + on-disk bytes) + schema.

    ``version`` measures that version rather than the newest, for a caller that knows which commit was its write.
    """
    ds = lance.dataset(uri, version=version, storage_options=storage_options, session=shared_lance_session())
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


def measure_stage(from_uri: str, to_uri: str, storage_options: dict[str, str], *, version: int | None = None) -> WriteResult:
    """Measure a stage ANOTHER engine wrote (the Ray job) and reconstruct its input→output column edges.

    ``version`` is the commit the run's commit marker named (CP-029), when its outcome found one: the measurement then
    describes that commit even if another (a compaction) landed after it. ``None`` measures the newest version.

    The distributed path writes the downstream dataset out-of-process (``scripts/ray_stage_job.py``), so
    nothing here ever sees the transformed table — a bare :func:`measure` would return an empty
    ``column_map`` and the emit would drop the ``columnLineage`` facet, leaving the field-to-field graph (#1)
    dead exactly where production runs. The Ray job writes the SAME columns as :func:`transform_stage`
    (upstream columns carried forward + the ``stage`` stamp + whatever the blob content derived), so those
    edges are recoverable from the two ON-DISK schemas alone: an output column that already exists upstream
    is IDENTITY, an artifact column that does not is TRANSFORMATION from the blob column the deriver
    dispatches on. Schema-only — no payload is re-read.

    The job writes through the same `service_kit.lakehouse.tier_write` as the in-process lane, so the
    ``lineage`` JSONB column, its JSON scalar index and the run's commit marker all land in the job's own
    commits; nothing here writes. ``version`` is the marked data commit, which the index build follows: a
    newest-version read would name that `CreateIndex` instead (measured 2026-09-11: all 253 stage-authored
    producer edges in the estate sat on a `CreateIndex` version).
    """
    upstream_schema = lance.dataset(from_uri, storage_options=storage_options, session=shared_lance_session()).schema
    result = measure(to_uri, storage_options, version=version)
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


def seed_bronze(uri: str, storage_options: dict[str, str], *, rows: int = 8, dataset_id: str | None = None) -> WriteResult:
    """Seed a small synthetic ``bronze$events`` dataset — the fake medallion-producer ingest at the head of the
    cascade (R23: the producer writes the first governed tier directly; there is no raw dataset).

    Carries the ``stage`` stamp the retired raw→bronze stage runner used to apply (merged into the bronze ingest
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
    # data_storage_version="2.2" — the current Lance format (blob v2 + Map need it). pylance 12.0.0 defaults
    # to it (measured 2026-09-25) and pylance <= 11 to 2.1, so it is named rather than left to whichever
    # pylance the image carries. enable_stable_row_ids — `_rowid` stays constant across compaction, which rewrites
    # fragments and invalidates row ADDRESSES. Stable row ids are CREATE-TIME-ONLY and cannot be turned on
    # later (`lance_docs/file_format.md:4011-4013`), which is why `ingest/catalog.py::A14` REFUSES a governed
    # dataset that lacks them rather than repairing it.
    #
    # THE RE-SEED MERGES ON `id` AND DOES NOT OVERWRITE, because overwrite re-mints every `_rowid` and
    # the estate's provenance rests on them. Measured on pylance 10.0.0: an overwrite of a stable-row-id
    # dataset moves `_rowid` [0,1,2] -> [3,4,5]; `merge_insert("id")` leaves it [0,1,2]. Measured on the
    # deployed estate 2026-09-06: bronze held 8 rows across 20 versions with live `_rowid` [2004..2011]
    # while silver's `source_rowid` still read [88..95] — 8 of 8 references dangling, the whole chain
    # D1 makes mandatory for impact analysis resolving to nothing, reported by nothing.
    #
    # Idempotence is unchanged: a re-seed of the same rows updates rows that are already there and
    # inserts none, so it stays the no-op it was. What it stops doing is re-minting identity to do it.
    if dataset_exists(uri, storage_options):
        # `when_not_matched_by_source_delete` is what makes this a FULL SYNC rather than an upsert, so
        # the tier still means "this run's output IS the whole dataset" — a row the seed no longer
        # produces is removed, exactly as overwrite removed it. Measured on pylance 10.0.0: source
        # dropping id=3 deletes it while id=1 keeps `_rowid` 0. Same semantics, surviving identity.
        open_to_commit(uri, storage_options).merge_insert(
            "id"
        ).when_matched_update_all().when_not_matched_insert_all().when_not_matched_by_source_delete().execute(declare_dataset_id(table, dataset_id))
        # A merge carries ROWS, not schema metadata (see `ensure_declared_dataset_id`), so the stamp on
        # the source table above reaches the dataset only on the create branch. This is the other half.
        ensure_declared_dataset_id(uri, dataset_id or "", storage_options, session=shared_lance_session())
    else:
        lance.write_dataset(  # noqa: TID251
            declare_dataset_id(table, dataset_id),
            uri,
            mode="create",
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


log = logging.getLogger(__name__)


def existing_row_count(uri: str, storage_options: dict[str, str]) -> int | None:
    """Rows at ``uri`` right now, or ``None`` when there is nothing there yet.

    ``None`` on any failure, deliberately: it means "no comparable predecessor", which the promotion
    band reads as a FIRST PROMOTION and therefore as a reason to ASK. A destination we cannot read is
    given a person's attention rather than a silent promote.
    """
    try:
        return int(lance.dataset(uri, storage_options=storage_options, session=shared_lance_session()).count_rows())
    except Exception as exc:  # noqa: BLE001 — any unreadable destination is "no predecessor", never a failure of the write
        # LOUD, because `None` is about to be read as FIRST_PROMOTION and asked about. Without this the
        # review says "first promotion" whether the dataset genuinely has no predecessor or we simply
        # could not read it — two very different situations wearing one outcome.
        log.warning("medallion_predecessor_unreadable", extra={"uri": uri, "error": str(exc)[:200]})
        return None


def transform_stage(
    from_uri: str,
    to_uri: str,
    storage_options: dict[str, str],
    *,
    stage: str,
    lineage: LineageDoc | None = None,
    dataset_id: str | None = None,
    version_floor: int | None = None,
    cardinality: str = ONE_TO_ONE,
    marker: CommitMarker | None = None,
) -> WriteResult:
    """Read the upstream Lance dataset, transform it in this process, and land it on the downstream tier.

    THE ROWS ARE THIS ENGINE'S; THE WRITE IS THE CASCADE'S. This function produces the transformed rows: every stage
    stamps the ``stage`` provenance column in place, threads ``source_rowid`` (minted at the first derive from the
    bronze ``_rowid``, carried thereafter), carries blob columns of any media kind through intact, derives what the
    blob content supports (`derivers.deriver_for`), and stamps this run's ``lineage`` document as a column of the
    same rows, so provenance lands in the same commit as the data (R26). Everything from those rows to the run's one
    marked commit is `service_kit.lakehouse.tier_write`, the module the Ray stage job writes through too: the window
    decision, create-or-converge, widening, the declared dataset id, the governance labels, the lineage index and the
    stage contract.

    A BLOB UPSTREAM IS PRODUCED AS A STREAM OF SLICES ([[CP-051]]), the Ray media producer's shape: one bounded scan
    in ``MEDALLION_STAGE_BATCH_ROWS`` slices, each carried, stamped and derived on its own, handed to the write as a
    re-runnable stream that lands one slice at a time. The stage holds a slice, never the tier, so an upstream larger
    than the pod's memory limit converges under it. A TABULAR upstream is read as one table, which the delta lane
    requires (`tier_write.write_tier` converges a bounded window from a table).

    ``version_floor`` is the order's delta boundary: when `tier_write.plan_window` keeps it, only the upstream rows
    changed since it are read and merged, and the upstream's deletions in the window are retracted. ``marker`` is the
    run's commit marker, carried by its last data commit.

    Returns the marked data version plus the measured output statistics for the emit; an empty delta, which commits
    no data, reports the destination's current version.

    SINGLE-BASE BY DESIGN (P2.1, docs/adr/0005-p2-1-single-base-cascade-write.md): the cascade writes to ONE root per
    stage and does not distribute a stage table across #3-B multi-base ``data_bases``. Multi-base registers its bases
    at create time only (``initial_bases``), a tier is created once and merged into thereafter, and the medallion
    already distributes physically at the per-zone bucket level.
    """
    ds = lance.dataset(from_uri, storage_options=storage_options, session=shared_lance_session())
    # BEFORE the write: what the destination holds now is the promotion band's only honest comparison point.
    previous_rows = existing_row_count(to_uri, storage_options)
    target = tier_write.TierTarget(uri=to_uri, storage_options=storage_options, dataset_id=dataset_id or "", cardinality=cardinality, marker=marker)
    window = tier_write.plan_window(ds, target, version_floor, session=shared_lance_session())
    blob_cols = blobs.blob_field_names(ds.schema)
    rows: tier_write.RowSource
    if blob_cols:
        if window.row_filter is not None:
            raise ValueError(f"a blob upstream converges whole; {ds.uri} was read under the window {window.row_filter!r}")
        settings = get_settings()
        rows, out_schema = _blob_slices(
            ds, stage, blob_cols, lineage, dataset_id, batch_rows=settings.stage_batch_rows, io_buffer_bytes=settings.stage_io_buffer_mb << 20
        )
        out_names = out_schema.names
    else:
        table = _stamp_stage(_drop_inherited_lineage(ds.to_table(with_row_id=True, filter=window.row_filter)), stage, stable_row_ids=ds.has_stable_row_ids)
        if lineage is not None:
            table = table.append_column(pa.field(_LINEAGE_COLUMN, pa.json_()), _lineage_column(lineage, table.num_rows))
        rows, out_names = table, table.column_names
    written = tier_write.write_tier(ds, rows, window, target, session=shared_lance_session())
    result = measure(to_uri, storage_options, version=written.version).model_copy(update={"previous_row_count": previous_rows})
    # Declare the input→output column edges for the columnLineage facet (#1): the blob columns are this stage's
    # deriver sources. The stage runner attaches the single upstream dataset identity.
    result.column_map = _column_map(ds.schema, out_names, set(blob_cols))
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


def _blob_slices(
    ds: lance.LanceDataset,
    stage: str,
    blob_cols: list[str],
    lineage: LineageDoc | None,
    dataset_id: str | None,
    *,
    batch_rows: int,
    io_buffer_bytes: int,
) -> tuple[Callable[[], pa.RecordBatchReader], pa.Schema]:
    """A blob upstream's rows as a factory of one stream of carried, stamped and derived slices, and the slices' schema.

    A FACTORY, because `tier_write` re-plans a marked merge that lost a commit race and a consumed stream cannot be read
    twice. Each slice is one row-aligned scan batch: tabular and blob columns arrive together and a null payload stays
    on its own row (R27), so a null blob carries forward as null with null artifacts.

    MANAGED VS EXTERNAL is decided by the upstream's registered base (`blobs.external_base_of`). A managed upstream's
    bytes exist nowhere else, so the scan reads them (``blob_handling="all_binary"``) and the slice carries them. An
    external upstream is scanned as descriptors and carried by POINTER, so a tier costs a few KB instead of another
    corpus (§4.1/§4.2, change 3); its rows the dataset itself holds are read by row id (`blobs.carried_blob_values`),
    and its bytes are read for a deriver only when one matched (:func:`_deriver_of`).

    THE SCAN IS BOUNDED BY THE SLICE, not by Lance's defaults: ``batch_size`` rows decoded at a time, one batch and one
    fragment of read-ahead, and ``io_buffer_size`` bytes buffered from storage (``lance_docs/guide.md:3050-3071``: a
    scan holds up to ``2 * io_buffer_size + batch_size * threads``; the default buffer is 2 GB).
    """
    external_base = blobs.external_base_of(ds)
    carried = [f.name for f in ds.schema if f.name not in _RESTAMPED_COLUMNS]
    derivation = _deriver_of(ds, blob_cols)

    def scanner(**kwargs: Any) -> lance.LanceScanner:
        return ds.scanner(
            columns=carried,
            blob_handling=tier_write.blob_handling(external_base),
            with_row_id=True,
            batch_size=batch_rows,
            batch_readahead=1,
            fragment_readahead=1,
            io_buffer_size=io_buffer_bytes,
            **kwargs,
        )

    def produce(aligned: pa.Table) -> pa.Table:
        row_ids = aligned.column("_rowid").to_pylist()
        columns: dict[str, Any] = {}
        fields: list[pa.Field] = []
        for name in carried:
            if name in blob_cols:
                field, columns[name] = tier_write.carried_blob_column(ds, name, aligned.column(name), row_ids, external_base)
                fields.append(field)
            else:
                fields.append(aligned.schema.field(name))
                columns[name] = aligned.column(name)
        out = _with_root_provenance(columns, fields, aligned.column("_rowid"), ds, stage=stage, rows=aligned.num_rows)
        if derivation is not None:
            column, deriver = derivation
            out = deriver(out, _slice_payloads(ds, aligned, column, row_ids, external_base))
        if lineage is not None:
            out = out.append_column(pa.field(_LINEAGE_COLUMN, pa.json_()), _lineage_column(lineage, out.num_rows))
        return declare_dataset_id(out, dataset_id)

    # The schema of an empty slice, so a source of zero rows still creates the tier with every column it would carry.
    schema = produce(scanner(limit=0).to_table()).schema

    def stream() -> pa.RecordBatchReader:
        slices = (out for raw in scanner().to_batches() for out in produce(pa.Table.from_batches([raw])).to_batches())
        return pa.RecordBatchReader.from_batches(schema, slices)

    return stream, schema


def _slice_payloads(ds: lance.LanceDataset, aligned: pa.Table, column: str, row_ids: list[int], external_base: str | None) -> list[bytes | None]:
    """One slice's payload bytes for the deriver, row-aligned, ``None`` where the payload is null.

    A managed slice was scanned as bytes. An external slice holds descriptors, so its bytes are read by row id:
    ``read_blobs`` keeps a null row's slot, answering ``(address, None)`` (measured on pylance 12.0.0: ids ``[0, 99]``
    over a null row 0 and an external row 99 answered ``[(0, None), (<address>, 1000 bytes)]``).
    """
    if external_base is None:
        return aligned.column(column).to_pylist()
    if not row_ids:
        return []
    return [payload for _, payload in ds.read_blobs(column, ids=row_ids, preserve_order=True)]


def _deriver_of(ds: lance.LanceDataset, blob_cols: list[str]) -> tuple[str, Deriver] | None:
    """The blob column this stage derives artifacts from, and its deriver; ``None`` when nothing derives.

    Decided ONCE, before the stream, because the derived columns are part of the schema every slice must share. The
    source is the first blob column by name, and its first NON-NULL payload decides: a failed harvest writes a null
    blob (R27), so row 0 cannot speak for the column. A tier that already carries the artifacts derives nothing, so a
    later hop carries them forward instead of appending a duplicate column.

    THE PROBE READS ONE PAYLOAD. It scans the column's descriptors (default blob handling, which reads no payload
    bytes) under ``filter="<col> IS NOT NULL", limit=1``, then reads that one row's bytes by row id. Measured on
    pylance 12.0.0 over 99 null rows followed by external payloads whose objects past the first had been deleted: the
    probe answered row 99 and read only its object, where a byte-reading scan of the column raised ``Not found`` on the
    first deleted one.
    """
    if not blob_cols or any(name in ds.schema.names for name in ARTIFACT_COLUMNS):
        return None
    column = min(blob_cols)
    first = ds.scanner(columns=[column], filter=f"{column} IS NOT NULL", limit=1, with_row_id=True).to_table()
    if not first.num_rows:
        return None
    payload = next(payload for _, payload in ds.read_blobs(column, ids=first.column("_rowid").to_pylist()))
    deriver = deriver_for(payload) if payload is not None else None
    return (column, deriver) if deriver is not None else None


def open_to_commit(uri: str, storage_options: dict[str, str]) -> lance.LanceDataset:
    """Open ``uri`` for a commit with Lance's commit-path auto-cleanup disarmed first ([[LH-245]]).

    Lance deletes versions inside any commit whose resulting manifest carries ``lance.auto_cleanup.*``
    config, past every hold and under this lane's static key (:mod:`service_kit.lakehouse.auto_cleanup`).
    The disarm is a config-only commit made only when a key is present, and every commit this lane then
    makes on the handle, or on a reopen, builds on the disarmed manifest.
    """
    dataset = lance.dataset(uri, storage_options=storage_options, session=shared_lance_session())
    if removed := auto_cleanup.disarm(dataset):
        log.warning("medallion_auto_cleanup_disarmed_before_commit", extra={"uri": uri, "keys": removed})
    return dataset


def dataset_exists(uri: str, storage_options: dict[str, str]) -> bool:
    """Whether `uri` already holds a dataset — the create-vs-overwrite question `initial_bases` asks.

    A read, not a stat: an object store has no directories, and `Path("s3://b/k")` collapses to a
    relative path (the same trap `ingest.catalog.ensure_at` documents).
    """
    try:
        lance.dataset(uri, storage_options=storage_options, session=shared_lance_session())
    except Exception:  # noqa: BLE001 — absent, unreadable, or not a dataset: all mean "create"
        return False
    return True


def _drop_inherited_lineage(table: pa.Table) -> pa.Table:
    """Drop an upstream ``lineage`` cell so it cannot survive into this stage's output.

    ``stage`` is re-stamped in place by :func:`_stamp_stage` (preserving column order); ``lineage`` is
    dropped instead because it is appended fresh only when the caller supplies this run's document — a
    stage invoked without one must write NO lineage column rather than the parent's.
    """
    return table.drop_columns([_LINEAGE_COLUMN]) if _LINEAGE_COLUMN in table.column_names else table


def _with_root_provenance(
    columns: dict[str, Any], fields: list[pa.Field], row_ids: pa.ChunkedArray, ds: lance.LanceDataset, *, stage: str, rows: int
) -> pa.Table:
    """The carried columns plus `source_rowid` and `stage`, root provenance minted by the shared stamp.

    A carried `source_rowid` came through the caller's loop as a plain upstream column and is kept; at
    the first derive off bronze it is minted from the just-read `_rowid` of the same scan, which
    `carry_source_rowid` refuses when the upstream has no stable row ids. `_rowid` is not persisted.
    """
    from service_kit.lakehouse.stage_stamp import carry_source_rowid

    carried = pa.table({**columns, "_rowid": row_ids}, schema=pa.schema([*fields, pa.field("_rowid", row_ids.type)]))
    out = carry_source_rowid(carried, stable_row_ids=ds.has_stable_row_ids)
    return out.append_column(pa.field(_STAGE_COLUMN, pa.string()), pa.array([stage] * rows, pa.string()))


def _stamp_stage(table: pa.Table, stage: str, *, stable_row_ids: bool) -> pa.Table:
    """Set (or append) the ``stage`` provenance column — delegated to the shared stamp.

    IN PLACE when the column exists, and that is the half which had drifted from the Ray driver:
    dropping and re-appending moves the column to the end, and `write_dataset(mode="overwrite")` takes
    the table's schema as the dataset's — so the same lane written two ways left unequal schemas.
    """
    from service_kit.lakehouse.stage_stamp import stamp_stage

    return stamp_stage(table, stage=stage, stable_row_ids=stable_row_ids)
