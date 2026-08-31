"""The dummy silver transform — trivial OUTPUT, real MECHANICS.

The point of this runner is that everything around the transform is production-shaped while the
transform itself needs no GPU, no model download, and no network. What it computes (a checksum, a
word count, an eight-float fake embedding) is deliberately meaningless. What it EXERCISES is not:

* a **CDF delta read** — `_row_created_at_version` bounded by the published version, so the mover
  processes exactly the new rows rather than rescanning the tier (D1);
* a **merge_insert on a stable id**, so a redelivered trigger converges instead of duplicating (E2);
* **source_rowid carried, bytes NOT copied** — silver stores a reference into bronze and consumers
  resolve payloads from there (D2). Copying blobs forward is the measured defect this avoids;
* a **catalog-registered commit carrying the run id**, which is how a died-after-commit job is
  reconciled from storage truth and how the code version binds to the data version in lineage.

So a green dummy lane proves the lane, not the model. That is the whole reason it exists: A11 must be
runnable on a laptop and in CI, or it will not be run, and an unrun end-to-end test is indistinguishable
from a broken one.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pyarrow as pa

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Width of the fake embedding. Eight, not 768: the column must be VECTOR-SHAPED so the silver schema
#: and any later index build are exercised, but nothing here pretends to be a model.
EMBED_DIM = 8

SILVER_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        # D2: a REFERENCE into bronze, never the bytes. Consumers resolve payloads via take_blobs /
        # read_blobs against bronze's stable row id. Carrying the bytes forward would make every tier
        # hold a complete copy — the measured defect in the current cascade.
        # uint64, matching Lance's own stable row id. int64 here was a DIFFERENT column wearing the
        # right name: present, non-null, and passing every count-based check the platform runs.
        pa.field("source_rowid", pa.uint64()),
        # The two columns that make a row traceable, beside the parent id. This runner cannot import
        # the platform's definition — it is sealed, and declares only `storage` and `pylance` — so it
        # SATISFIES the contract rather than importing it, and the catalog's publish door is the
        # authority that refuses a tier which does not. That is the seam working as intended: a runner
        # is told it is wrong at the door, not coupled to the rule.
        pa.field("stage", pa.string()),
        pa.field("lineage", pa.string()),
        pa.field("checksum", pa.string()),
        pa.field("word_count", pa.int64()),
        pa.field("embedding", pa.list_(pa.float32(), EMBED_DIM)),
    ]
)


def _fake_embedding(digest: bytes) -> list[float]:
    """Eight floats derived from the checksum — deterministic, so a re-run converges byte for byte.

    Determinism is the property under test, not the values. A random embedding would make E5 replay
    produce a different silver from the same bronze, and the "replay converges to identical content
    hashes" assertion (A19) could never hold.
    """
    return [digest[i] / 255.0 for i in range(EMBED_DIM)]


def transform_batch(batch: pa.Table, *, stage: str = "silver", lineage: str = "") -> pa.Table:
    """bronze rows -> silver rows. Pure: no I/O, so it is testable without a cluster."""
    ids: list[int] = batch.column("id").to_pylist()
    if "_rowid" not in batch.column_names:
        # REFUSE, never invent. This read `else list(range(len(ids)))` — the row's POSITION, written
        # into the column that means "the bronze row this descends from". The result was queryable,
        # confident and false: asked which rows descend from a corrupted document, it answered with
        # whichever rows happened to sit at those offsets.
        #
        # An absent `_rowid` is a real fault with a known cause — the upstream was not created with
        # `enable_stable_row_ids=True`, which is a create-time-only flag — so it is reported rather
        # than papered over. Owner ruling D1, 2026-08-31: provenance is mandatory, and a fabricated
        # parent is worse than a missing one.
        raise ValueError("upstream carries no `_rowid`: provenance cannot be derived, and inventing it would be a false claim")
    rowids: list[int] = batch.column("_rowid").to_pylist()
    payloads: Sequence[bytes | None] = batch.column("payload").to_pylist()

    checksums: list[str] = []
    counts: list[int] = []
    embeddings: list[list[float]] = []
    for payload in payloads:
        raw = payload or b""
        digest = hashlib.sha256(raw).digest()
        checksums.append(digest.hex())
        counts.append(len(raw.split()))
        embeddings.append(_fake_embedding(digest))

    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "source_rowid": pa.array(rowids, pa.uint64()),
            "checksum": pa.array(checksums, pa.string()),
            "word_count": pa.array(counts, pa.int64()),
            "embedding": pa.array(embeddings, pa.list_(pa.float32(), EMBED_DIM)),
            "stage": pa.array([stage] * len(ids), pa.string()),
            "lineage": pa.array([lineage] * len(ids), pa.string()),
        },
        schema=SILVER_SCHEMA,
    )
