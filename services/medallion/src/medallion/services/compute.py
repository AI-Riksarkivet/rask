"""The fake-Ray in-process Lance compute for the medallion cascade (the lance-ray seam, #25 / P1 #6).

Default OFF (``MEDALLION_COMPUTE_ENABLED``): the movers/producer stay dummy-emitters (lineage, no data).
When on, each stage does a **real** Lance write — the producer seeds ``raw_events``; each mover reads its
upstream Lance dataset, applies a stage transform, and writes the downstream one — so the emitted lineage
carries the **real** Lance version and the whole event-driven loop produces actual versioned data, not just
provenance.

This is the **same** ``read → transform → write → version`` contract a distributed Ray Data job
(``lance-ray`` on rask's KubeRay) fills in production; here it runs **in-process** so the cascade is
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
from common import blobs, schema
from lance import blob_array, blob_field
from pydantic import BaseModel, Field

from medallion.services.derivers import ARTIFACT_COLUMNS, derive_artifacts


_STAGE_COLUMN = "stage"
#: Row-level provenance: the stable ``_rowid`` of the RAW-zone row this output descends from. Minted at the
#: cascade head from the upstream row's reserved ``_rowid`` metacolumn (durable because every stage writes
#: ``enable_stable_row_ids=True``) and carried forward unchanged thereafter — so a gold row names the exact
#: source row it came from in ONE join, not a hop-by-hop walk. ``_rowid`` advances on overwrite, so this is a
#: snapshot taken at cascade-run time; a fresh cascade run over a re-seeded raw table re-captures it.
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
    #: never saw). Empty only where there is genuinely nothing to declare: the raw head (no upstream), the
    #: dummy compute-off emit, and a bare :func:`measure` — which is why a stage the Ray job wrote MUST be
    #: read back with ``measure_stage``, or its columnLineage facet silently disappears.
    column_map: list[tuple[str, str, str]] = Field(default_factory=list)


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
    """
    upstream_schema = lance.dataset(from_uri, storage_options=storage_options).schema
    result = measure(to_uri, storage_options)
    # result.fields IS the written schema (facet_fields of the just-measured dataset) — its names are all
    # the edge reconstruction needs on the output side, so the target is opened once, not twice.
    written_columns = [field["name"] for field in result.fields]
    result.column_map = _column_map(upstream_schema, written_columns, set(blobs.blob_field_names(upstream_schema)))
    return result


def seed_raw(uri: str, storage_options: dict[str, str], *, rows: int = 8) -> WriteResult:
    """Seed a small synthetic ``raw_events`` dataset — the fake lance-ray ingest at the head of the cascade.

    Overwrites any existing dataset (idempotent re-seed) and returns the resulting Lance version + the
    measured output statistics (rows + on-disk bytes) the emit records as an ``outputStatistics`` facet.
    """
    table = pa.table(
        {
            "id": pa.array(list(range(rows)), pa.int64()),
            "payload": pa.array([f"event-{i}" for i in range(rows)]),
        }
    )
    # data_storage_version="2.2" — the current Lance format (blob v2 + Map need it; pylance 8 still
    # defaults to 2.1). Overwrite-mode upgrades a pre-existing 2.1 dataset forward on the next run.
    # enable_stable_row_ids — row _rowid stays constant across compaction (which rewrites fragments and
    # invalidates row ADDRESSES). This is a CREATE-TIME-ONLY flag: it cannot be turned on later, so we set it
    # at the cascade head to keep durable row identity available (e.g. to key blob carry-forward by _rowid if
    # a stage ever gains append/upsert). Free on top of overwrite; the positional read path is unaffected.
    lance.write_dataset(
        table,
        uri,
        mode="overwrite",
        storage_options=storage_options,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return measure(uri, storage_options)


def transform_stage(from_uri: str, to_uri: str, storage_options: dict[str, str], *, stage: str) -> WriteResult:
    """Read the upstream Lance dataset, transform, write the downstream dataset (the generic stage).

    Every stage stamps the ``stage`` provenance column (set, not appended twice, so re-running over an
    already-stamped upstream replaces the value), threads the row-level ``source_rowid`` provenance column
    (minted at the head from the upstream ``_rowid``, carried forward thereafter — so a gold row names the
    exact raw row it descends from), carries blob columns of ANY media kind through intact
    (``_carry_forward``), and derives whatever the blob CONTENT supports (``derive_artifacts`` — image →
    thumbnail+embedding, unrecognised → untouched, tabular → no-op). Returns the new downstream Lance
    version + the measured output statistics (rows + on-disk bytes) for the emit.

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
    # 2.2 + stable row ids like seed_raw: every dataset the cascade writes is on the current format (so a blob
    # column never trips "Blob v2 requires file version >= 2.2" mid-cascade) and keeps durable row identity.
    lance.write_dataset(
        out,
        to_uri,
        mode="overwrite",
        storage_options=storage_options,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    result = measure(to_uri, storage_options)
    # Declare the input→output column edges for the columnLineage facet (#1) — blob_payloads' keys ARE this
    # stage's blob columns (the deriver source). The mover attaches the single upstream dataset identity.
    result.column_map = _column_map(ds.schema, out.column_names, set(blob_payloads))
    return result


def _column_map(in_schema: pa.Schema, out_names: list[str], blob_cols: set[str]) -> list[tuple[str, str, str]]:
    """This stage's input→output column edges: ``(out_field, in_field, transformation_subtype)``.

    The generic transform carries every upstream column forward (``IDENTITY``, keyed on the same name) and
    derives blob artifacts (``thumbnail``/``embedding``) from their source blob column
    (``TRANSFORMATION``). The ``stage`` provenance stamp is a constant with no input, so it gets no edge.
    A carried-forward artifact (a later stage that didn't re-derive) is IDENTITY like any other column.

    Keyed on NAMES only — the upstream schema plus the names of the written columns — so the same rules
    classify a table this process built (:func:`transform_stage`) and one only its on-disk schema is known
    for (:func:`measure_stage`, the Ray path).
    """
    in_names = {f.name for f in in_schema}
    deps: list[tuple[str, str, str]] = [(name, name, "IDENTITY") for name in out_names if name != _STAGE_COLUMN and name in in_names]
    # source_rowid is minted at the cascade head from the upstream row's reserved ``_rowid`` metacolumn (root
    # provenance) — declare that as its input edge. Once it exists it is carried forward like any column, so
    # a later stage (source_rowid in BOTH schemas) is already handled as IDENTITY by the rule above.
    if _SOURCE_ROWID_COLUMN in set(out_names) and _SOURCE_ROWID_COLUMN not in in_names:
        deps.append((_SOURCE_ROWID_COLUMN, "_rowid", "IDENTITY"))
    if blob_cols:
        source = min(blob_cols)  # matches derivers' ``min(blob_payloads)`` dispatch — deterministic source
        deps += [(artifact, source, "TRANSFORMATION") for artifact in ARTIFACT_COLUMNS if artifact in out_names and artifact not in in_names]
    return deps


def _carry_forward(ds: lance.LanceDataset, stage: str) -> tuple[pa.Table, dict[str, list[bytes]]]:
    """Read the upstream table and stamp the ``stage`` column, carrying any blob-v2 column through intact.

    A plain ``to_table()`` demotes a blob column to its descriptions struct (tagged with the legacy
    ``lance-encoding:blob`` key), which the 2.2 write then rejects — so blob columns are re-materialised
    via ``read_blobs`` + ``blob_array``. A stage with no blob column keeps the cheap straight-through
    path. Returns the stamped table AND the materialised blob payloads per column, so a media stage can
    derive artifacts without a second ``read_blobs`` pass.
    """
    blob_cols = blobs.blob_field_names(ds.schema)
    if not blob_cols:
        return _stamp_stage(_carry_source_rowid(ds.to_table(with_row_id=True)), stage), {}

    # Full-materialise each blob column into memory (read_blobs by positional indices 0..N-1) — fine for
    # this in-process fake-Ray stand-in over the cascade's small overwrite-written datasets (contiguous
    # row ids); a distributed job would stream instead. `range(rows)` aligns with `to_table()` only because
    # the cascade is overwrite-only (no soft-deleted rows to shift positions).
    rows = ds.count_rows()
    # with_row_id so the head can mint source_rowid from the SAME scan the rows come from (positionally
    # aligned with read_blobs(range(rows)); a carried source_rowid is a plain column already in this read).
    plain = ds.to_table(
        columns=[f.name for f in ds.schema if f.name not in blob_cols and f.name != _STAGE_COLUMN],
        with_row_id=True,
    )
    columns: dict[str, Any] = {}
    fields: list[pa.Field] = []
    blob_payloads: dict[str, list[bytes]] = {}
    for f in ds.schema:
        if f.name == _STAGE_COLUMN:
            continue  # re-stamped below so the value reflects this stage, not the upstream's
        if f.name in blob_cols:
            payloads = [payload for _addr, payload in ds.read_blobs(f.name, indices=list(range(rows)))]
            blob_payloads[f.name] = payloads
            fields.append(blob_field(f.name))
            columns[f.name] = blob_array(payloads)
        else:
            fields.append(plain.schema.field(f.name))
            columns[f.name] = plain.column(f.name)
    # Root provenance: a carried source_rowid came through the loop above (a plain upstream column); at the
    # cascade head it is minted here from the just-read _rowid (same aligned scan). _rowid is not persisted.
    if _SOURCE_ROWID_COLUMN not in columns:
        fields.append(pa.field(_SOURCE_ROWID_COLUMN, pa.uint64()))
        columns[_SOURCE_ROWID_COLUMN] = plain.column("_rowid").cast(pa.uint64())
    fields.append(pa.field(_STAGE_COLUMN, pa.string()))
    columns[_STAGE_COLUMN] = pa.array([stage] * rows, pa.string())
    return pa.table(columns, schema=pa.schema(fields)), blob_payloads


def _carry_source_rowid(table: pa.Table) -> pa.Table:
    """Ensure ``source_rowid`` holds the stable _rowid of the RAW row this output descends from (root
    provenance). An upstream that already carries it (a later stage) keeps it; the cascade head mints it from
    the reserved ``_rowid`` metacolumn of the row just read. ``_rowid`` itself is never persisted (it is a
    reserved name and would advance on the next overwrite). Input MUST be read ``with_row_id=True``.

    HEAD DETECTION IS HEURISTIC (absence of ``source_rowid``), not positional. In the steady state only the
    raw head lacks it, so this is exact. During a MIXED-VERSION ROLLOUT a mid-cascade dataset written by
    pre-#38a code also lacks it; a stage reading such an upstream mints from the IMMEDIATE parent's _rowid
    (parent, not root) for that one cycle — the best available id when the root was never captured — and
    self-heals on the next full run from raw. Acceptable because the cascade is overwrite-only and re-runs.
    """
    if _SOURCE_ROWID_COLUMN in table.column_names:
        return table.drop_columns(["_rowid"]) if "_rowid" in table.column_names else table
    srid = table.column("_rowid").cast(pa.uint64())
    return table.drop_columns(["_rowid"]).append_column(pa.field(_SOURCE_ROWID_COLUMN, pa.uint64()), srid)


def _stamp_stage(table: pa.Table, stage: str) -> pa.Table:
    """Set (or append) the ``stage`` provenance column on ``table``."""
    field = pa.field(_STAGE_COLUMN, pa.string())
    marker = pa.array([stage] * table.num_rows, pa.string())
    if _STAGE_COLUMN in table.column_names:
        return table.set_column(table.schema.get_field_index(_STAGE_COLUMN), field, marker)
    return table.append_column(field, marker)
