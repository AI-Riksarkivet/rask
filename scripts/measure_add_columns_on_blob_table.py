"""Does add_columns rewrite a table that HAS a blob column?

The spec's changes 1 and 4 both depend on the answer and neither Lance source states it.
Method: build a 2.2 dataset with a real blob column, fingerprint every file on disk,
add two ordinary string columns, fingerprint again, and diff.
"""

import os
import pathlib
import tempfile

import lance
import pyarrow as pa
from lance import blob_array, blob_field


ROOT = str(pathlib.Path(tempfile.mkdtemp(prefix="ac_probe_")) / "probe.lance")

N = 300
# Mixed sizes so we exercise more than one storage semantic.
payloads = [os.urandom(200_000 if i % 3 == 0 else 900) for i in range(N)]

schema = pa.schema(
    [
        pa.field("id", pa.int64()),
        blob_field("payload", nullable=True),
        pa.field("stage", pa.string()),
    ]
)
tbl = pa.table(
    {"id": pa.array(range(N)), "payload": blob_array(payloads), "stage": pa.array(["bronze"] * N)},
    schema=schema,
)
ds = lance.write_dataset(tbl, ROOT, data_storage_version="2.2", enable_stable_row_ids=True)
print(f"created  v{ds.version}  rows={ds.count_rows()}")


def snapshot(tag: str) -> dict[str, int]:
    files: dict[str, int] = {}
    for p in pathlib.Path(ROOT).rglob("*"):
        if p.is_file():
            files[str(p.relative_to(ROOT))] = p.stat().st_size
    blobs = {k: v for k, v in files.items() if k.endswith(".blob")}
    data = {k: v for k, v in files.items() if k.endswith(".lance")}
    print(f"\n[{tag}] .lance data files: {len(data)}  ({sum(data.values()):,} B)")
    for k, v in sorted(data.items()):
        print(f"    {k}  {v:,}")
    print(f"[{tag}] .blob sidecars:    {len(blobs)}  ({sum(blobs.values()):,} B)")
    for k, v in sorted(blobs.items()):
        print(f"    {k}  {v:,}")
    return files


before = snapshot("BEFORE")

# The operation under test: two ordinary columns, computed per fragment.
ds.add_columns(
    {
        "ocr": "CAST(id AS STRING)",
        "summary": "CAST(id AS STRING)",
    }
)
ds = lance.dataset(ROOT)
print(f"\nadd_columns -> v{ds.version}  cols={ds.schema.names}")

after = snapshot("AFTER")

print("\n================ VERDICT ================")
kept = {k for k in before if k in after and before[k] == after[k]}
changed = {k for k in before if k in after and before[k] != after[k]}
gone = set(before) - set(after)
new = set(after) - set(before)
print(f"  carried forward byte-identical : {len(kept)}")
print(f"  MODIFIED in place              : {len(changed)}  {sorted(changed) if changed else ''}")
print(f"  REMOVED                        : {len(gone)}  {sorted(gone) if gone else ''}")
print(f"  NEW                            : {len(new)}")
for k in sorted(new):
    print(f"      + {k}  {after[k]:,}")

b_before = {k: v for k, v in before.items() if k.endswith(".blob")}
b_after = {k: v for k, v in after.items() if k.endswith(".blob")}
print(f"\n  blob sidecars rewritten?       : {'YES' if b_before != b_after else 'NO - identical'}")
print(f"  payload bytes re-written?      : {sum(b_after.values()) - sum(b_before.values()):,} B delta")
