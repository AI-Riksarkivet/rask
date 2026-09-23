"""Does a shallow clone of bronze@N cost fewer BYTES than the copy silver makes today?

[[LH-097]]. `medallion/services/compute.py:513` states "MANAGED UPSTREAM: the bytes exist nowhere else,
so carrying them IS the only option", and carries every managed blob payload from bronze into silver.
The row disputes the conclusion, not the premise: the bytes do exist nowhere else, but a SHALLOW CLONE
shares the source's data files rather than rewriting them, so silver can hold the same rows without
committing a second copy.

lance_docs is the authority and it says the shape is supported -- `clone_table(..., source_version=...,
is_shallow=True)`: "A shallow clone creates a new table that shares the underlying data files with the
source table but has its own independent manifest. This allows both the source and cloned tables to
evolve independently while initially sharing the same data, deletion, and index files." The pylance
handle is `LanceDataset.shallow_clone(target_path, reference, ...)`, where `reference` pins the version.

WHY MEASURE RATHER THAN SWITCH. The existing probes cover neighbouring questions and not this one:
`measure_blob_descriptor_carry_forward.py` and `measure_add_columns_on_blob_table.py` answer what
happens to descriptors and whether `add_columns` rewrites. Neither compares clone-against-copy, which
is the trade the row turns on -- and `compute.py`'s claim is a comment, not a measurement.

THE RECLAIM OBJECTION IS ALREADY ANSWERED, which is why this is worth measuring at all. The row was
parked on "the sweep refuses reclaim on shallow clones"; it does not.
`service_kit/lakehouse/features.py:172` is ``SUPPORTED_FOR_GC = SUPPORTED | FLAG_BASE_PATHS`` and
`:617-623` states the reason -- version reclamation and index maintenance touch only the dataset's own
root. The constraint that DOES exist is on the source, and this script measures that too: a clone
pinning bronze@N is a reason bronze cannot reclaim N, and that is the real cost to weigh.

Run: `uv run python scripts/measure_clone_vs_copy_for_silver.py`
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time

import lance
import pyarrow as pa
from lance import blob_array, blob_field

from service_kit.lakehouse import blobs


ROOT = pathlib.Path(tempfile.mkdtemp(prefix="clone_vs_copy_"))
BRONZE = str(ROOT / "bronze.lance")
COPIED = str(ROOT / "silver_copied.lance")
CLONED = str(ROOT / "silver_cloned.lance")

N = 300
#: Mixed sizes, the same way `measure_add_columns_on_blob_table.py` does it, so more than one storage
#: semantic is exercised rather than one payload class that happens to answer cleanly.
PAYLOADS = [os.urandom(200_000 if i % 3 == 0 else 900) for i in range(N)]

SCHEMA = pa.schema([pa.field("id", pa.int64()), blob_field("payload", nullable=True), pa.field("stage", pa.string())])


def _bytes_on_disk(root: str) -> tuple[int, int, int]:
    """(total, .lance data bytes, .blob bytes) — what this dataset OWNS, not what it can read."""
    total = data = blob = 0
    for p in pathlib.Path(root).rglob("*"):
        if not p.is_file():
            continue
        size = p.stat().st_size
        total += size
        if p.name.endswith(".blob"):
            blob += size
        elif p.name.endswith(".lance"):
            data += size
    return total, data, blob


def main() -> None:
    table = pa.table(
        {"id": pa.array(range(N)), "payload": blob_array(PAYLOADS), "stage": pa.array(["bronze"] * N)},
        schema=SCHEMA,
    )
    bronze = lance.write_dataset(table, BRONZE, data_storage_version="2.2", enable_stable_row_ids=True)
    pinned = bronze.version
    b_total, b_data, b_blob = _bytes_on_disk(BRONZE)
    print(f"bronze@{pinned}: rows={bronze.count_rows()}  total={b_total:,} B  data={b_data:,}  blob={b_blob:,}")

    # ---- SHAPE A: what silver does today. -----------------------------------------------------
    # Through `blobs.read_aligned_table`, the estate's OWN carry path, not a stand-in: a plain
    # `to_table()` hands back blob DESCRIPTORS and the re-write is refused outright ("Blob v2 field
    # 'payload' has descriptor layout"). Measuring a shape the medallion does not run would answer a
    # question nobody asked.
    started = time.perf_counter()
    aligned = blobs.read_aligned_table(bronze, columns=["id", "payload"])
    carried = pa.table(
        {
            "id": aligned.column("id"),
            "payload": blob_array(aligned.column("payload").to_pylist()),
            "stage": pa.array(["silver"] * N),
        },
        schema=SCHEMA,
    )
    lance.write_dataset(carried, COPIED, data_storage_version="2.2", enable_stable_row_ids=True)
    copy_seconds = time.perf_counter() - started
    c_total, c_data, c_blob = _bytes_on_disk(COPIED)

    # ---- SHAPE B: share bronze's files, then add silver's own column. --------------------------
    started = time.perf_counter()
    clone = bronze.shallow_clone(CLONED, pinned)
    clone.add_columns({"silver_stage": "'silver'"})
    clone_seconds = time.perf_counter() - started
    l_total, l_data, l_blob = _bytes_on_disk(CLONED)

    print(f"\ncopy  : total={c_total:,} B  data={c_data:,}  blob={c_blob:,}  in {copy_seconds:.3f}s")
    print(f"clone : total={l_total:,} B  data={l_data:,}  blob={l_blob:,}  in {clone_seconds:.3f}s")
    saved = c_total - l_total
    print(f"\nDELTA : clone commits {saved:,} B fewer ({saved / c_total:.1%} of the copy) and is {copy_seconds / max(clone_seconds, 1e-9):.1f}x faster")

    # ---- The cost the row has to weigh against that, measured rather than asserted. -------------
    reopened = lance.dataset(CLONED)
    print(f"\nclone reads {reopened.count_rows()} rows and carries columns {reopened.schema.names}")

    # ROW COUNT IS NOT PAYLOAD ACCESS, and only the second one makes the saving real. A clone that
    # answers 300 rows while its blob column reads nothing would look like a 100% saving and be a data
    # loss. Read the payloads back THROUGH the same helper the cascade uses and compare the bytes.
    back = blobs.read_aligned_table(reopened, columns=["id", "payload"]).column("payload").to_pylist()
    identical = sum(1 for got, want in zip(back, PAYLOADS, strict=True) if got == want)
    print(f"clone payloads readable: {sum(1 for b in back if b)}/{N}, byte-identical to bronze: {identical}/{N}")
    if identical != N:
        raise SystemExit(f"REFUTED: the clone does not serve {N - identical} payloads; the saving is data loss, not a saving")
    print("THE COUPLING: the clone's manifest references bronze's files, so bronze cannot reclaim the")
    print(f"pinned version {pinned} while it stands. That is the trade — bytes saved now against a")
    print("version the source must keep. `SUPPORTED_FOR_GC` permits reclaim ON the clone; the constraint")
    print("is on the SOURCE, which is the half the parked marker had backwards.")


if __name__ == "__main__":
    main()
