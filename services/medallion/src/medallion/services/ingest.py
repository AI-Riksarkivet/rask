"""Land external source objects into a bronze Lance blob table — the ingest head of the cascade.

Reads a provider-agnostic ``SourceAdapter`` (:mod:`service_kit.lakehouse.sources`) and writes each object's bytes as a
managed blob-v2 column at file format 2.2, keeping the object's source URI as a column so provenance
survives in the data itself (the caller emits the ``source -> bronze`` lineage edge from ``source_uris``).
Bronze is the FIRST governed tier (R23 — raw is the external world): the ``stage`` provenance stamp the
retired raw→bronze mover used to apply is written here at ingest, and ``source_rowid`` roots at these
bronze rows (minted by the first downstream derive from bronze's stable ``_rowid``). The per-stage ML
then flows the blob forward (``compute._carry_forward``) and derives the silver artifacts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping
from itertools import chain

import lance
import pyarrow as pa
from lance import blob_array, blob_field
from pydantic import BaseModel

from service_kit.lakehouse import schema
from service_kit.lakehouse.sources import SourceAdapter, SourceObject


#: The ``stage`` provenance stamp every governed dataset carries — bronze gets it AT INGEST (R23: the
#: bronze head absorbed the retired raw→bronze mover); downstream movers re-stamp it per stage.
_STAGE_COLUMN = "stage"
_BRONZE_STAGE = "bronze"

#: The FIXITY column (#92). A hex SHA-256 over the bytes AS HARVESTED, computed once at ingest and
#: never recomputed — a digest recomputed from the stored copy would agree with itself no matter how
#: corrupt the copy is, which is the one thing fixity must not do.
#:
#: Bronze COPIES the bytes rather than pointing at them, which already makes the estate immune to the
#: source vanishing. What a copy does not solve is asserting, later, that the copy is still the bytes
#: that were harvested — bit-rot detection over the archival master is baseline OAIS practice, and
#: with no stored digest the estate had no way to make that assertion at all.
#:
#: `_carry_forward` reads every column generically, so this travels with the row into silver and gold
#: for free: a gold transcription can be traced to the exact page bytes it was read from.
_SHA256_COLUMN = "sha256"

_INGEST_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        blob_field("payload"),
        pa.field("source_uri", pa.string()),
        pa.field(_SHA256_COLUMN, pa.string()),
    ]
)

#: Extra STRING columns appended after the core ingest triple — ``{column_name: extractor(obj)}``,
#: applied in mapping order. A page lane uses this for grouping columns such as volume/page keys; the
#: media/events lanes pass none and stay byte-identical to the pre-P7a write.
type ExtraColumns = Mapping[str, Callable[[SourceObject], str]]


def _ingest_schema(extra_columns: ExtraColumns | None) -> pa.Schema:
    fields = list(_INGEST_SCHEMA)
    for name in extra_columns or {}:
        fields.append(pa.field(name, pa.string()))
    fields.append(pa.field(_STAGE_COLUMN, pa.string()))  # last, mirroring the movers' stamp position
    return pa.schema(fields)


class IngestResult(BaseModel):
    """What one ingest produced: the bronze version + rows, the source URIs (for the lineage edge), and the
    written schema facet (blob-aware, for the WROTE edge)."""

    version: int
    row_count: int
    source_uris: list[str]
    fields: list[dict[str, str]]


def _chunk_batch(chunk: list[SourceObject], first_id: int, extra_columns: ExtraColumns | None = None) -> pa.RecordBatch:
    """One chunk's rows as a RecordBatch; ``first_id`` keeps the positional id GLOBAL across chunks."""
    columns: dict[str, pa.Array] = {
        # Positional id — the cascade is OVERWRITE-ONLY and compute._carry_forward re-reads blobs by
        # the same range(rows); batches land in yield order within the single write, so the global
        # offset keeps id == position. If ingest ever gains true append mode, derive a stable id
        # from source_uri.
        "id": pa.array(range(first_id, first_id + len(chunk)), pa.int64()),
        "payload": blob_array([obj.data for obj in chunk]),
        "source_uri": pa.array([obj.uri for obj in chunk], pa.string()),
        # Over `obj.data` — the bytes as they arrived from the source, hashed BEFORE the write, not
        # read back from Lance afterwards. A digest taken from the stored copy would agree with that
        # copy however corrupt it is, which is precisely the failure fixity exists to catch.
        _SHA256_COLUMN: pa.array([hashlib.sha256(obj.data).hexdigest() for obj in chunk], pa.string()),
    }
    for name, extract in (extra_columns or {}).items():
        columns[name] = pa.array([extract(obj) for obj in chunk], pa.string())
    columns[_STAGE_COLUMN] = pa.array([_BRONZE_STAGE] * len(chunk), pa.string())
    return pa.record_batch(columns, schema=_ingest_schema(extra_columns))


def ingest_to_bronze(
    source: SourceAdapter,
    bronze_uri: str,
    storage_options: dict[str, str],
    *,
    max_objects: int = 10_000,
    max_total_bytes: int = 1 << 30,
    chunk_objects: int = 64,
    chunk_bytes: int = 64 << 20,
    extra_columns: ExtraColumns | None = None,
) -> IngestResult:
    """Write every object from ``source`` into a bronze blob-v2 table at 2.2 — ONE ATOMIC, BOUNDED-MEMORY
    commit via Lance's NATIVE streaming write.

    Raises ``ValueError`` on an empty source (checked by a peek BEFORE any write touches storage): an empty
    bronze is almost always a mis-set prefix, and silently "succeeding" with zero rows would report a false
    success (and an input-less lineage edge) up the cascade.

    LANCE-NATIVE STREAMING WRITE (2026-07-13 — replaced the hand-rolled overwrite-then-append multi-commit
    chunking after checking the Lance guide: ``write_dataset`` accepts ``Iterator[RecordBatch]`` + ``schema``
    and streams it with bounded memory, so the multi-commit loop was a DIY copy of a shipped feature).
    Objects are batched into RecordBatches — ``chunk_objects`` OR ``chunk_bytes``, WHICHEVER TRIPS FIRST:
    the object count alone would let 64 large images balloon one batch toward the whole total ceiling,
    so the byte bound is the real memory guarantee and the count bound is batching hygiene (many tiny
    objects still amortize into sensibly-sized batches). Memory high-water is ONE batch plus the
    accumulated URI strings, regardless of source size; fragment/file layout inside the commit is Lance's
    own job (``max_rows_per_file``/``max_bytes_per_file`` defaults). Neither knob is a Dapr/NATS
    constraint — the bus never carries payload bytes (claim-check: events are pointers).

    ONE COMMIT, ATOMIC (probed): the version bumps exactly once per ingest — the lineage WROTE edge and the
    readable result are the same handle — and a mid-ingest failure commits NOTHING: an aborted re-ingest
    leaves the PREVIOUS bronze fully readable (the old multi-commit path could not promise this — its first
    chunk had already overwritten). Orphaned data files from an abort carry no manifest and the next
    successful overwrite supersedes them.

    The CEILINGS remain the refusal guard (clear ``ValueError`` naming the env knob, mapped to 400 at the
    route) against a mis-pointed prefix: bounded memory does not make a million-object ingest a good idea.
    They are enforced INSIDE the batch generator, and Lance's FFI wraps a generator's exception in
    ``OSError`` — so the recorded ValueError is re-raised at the boundary to keep the contract.

    EXTERNAL-URI BLOBS deliberately NOT used here: blob v2 can point at external objects
    (``Blob.from_uri``) which would make ingest a zero-copy metadata write, but bronze must OWN its bytes —
    a pointer-only bronze dangles the moment the source bucket's lifecycle deletes or migrates an object
    (exactly the failure mode the reconcile dangling-pointer patrol exists for). Copying once at the ingest
    boundary is the cheap insurance that the lakehouse's ground truth is self-contained.
    """
    objects = iter(source.iter_objects())
    first = next(objects, None)
    if first is None:
        raise ValueError(f"source yielded no objects; refusing to write an empty bronze at {bronze_uri!r}")

    source_uris: list[str] = []
    ceiling_error: ValueError | None = None

    def batches() -> Iterator[pa.RecordBatch]:
        nonlocal ceiling_error
        chunk: list[SourceObject] = []
        chunk_size = 0
        total_bytes = 0
        next_id = 0
        for obj in chain([first], objects):
            chunk.append(obj)
            source_uris.append(obj.uri)
            total_bytes += len(obj.data)
            if len(source_uris) > max_objects:
                ceiling_error = ValueError(
                    f"source exceeds the ingest ceiling of {max_objects} objects (MEDALLION_INGEST_MAX_OBJECTS); narrow the source prefix or raise the ceiling"
                )
                raise ceiling_error
            if total_bytes > max_total_bytes:
                ceiling_error = ValueError(
                    f"source exceeds the ingest ceiling of {max_total_bytes} total bytes "
                    f"(MEDALLION_INGEST_MAX_TOTAL_BYTES); narrow the source prefix or raise the ceiling"
                )
                raise ceiling_error
            chunk_size += len(obj.data)
            if len(chunk) >= chunk_objects or chunk_size >= chunk_bytes:
                yield _chunk_batch(chunk, next_id, extra_columns)
                next_id += len(chunk)
                chunk = []
                chunk_size = 0
        if chunk:
            yield _chunk_batch(chunk, next_id, extra_columns)

    try:
        dataset = lance.write_dataset(
            batches(),
            bronze_uri,
            schema=_ingest_schema(extra_columns),
            mode="overwrite",
            storage_options=storage_options,
            data_storage_version="2.2",
            # enable_stable_row_ids (create-time-only) — durable _rowid across compaction.
            enable_stable_row_ids=True,
        )
    except Exception:
        if ceiling_error is not None:
            raise ceiling_error from None
        raise
    return IngestResult(
        version=int(dataset.version),
        row_count=dataset.count_rows(),
        source_uris=source_uris,
        fields=schema.facet_fields(dataset.schema),
    )
