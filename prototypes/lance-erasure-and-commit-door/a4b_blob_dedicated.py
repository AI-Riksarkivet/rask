import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lance import blob_array, blob_field

from common import *


MARK = b"ZQXSUBJECTBLOBZQX"


def payload(tag, size):
    return (tag.encode() * (size // len(tag) + 1))[:size]


def blobs(p):
    return [(f.split("/")[-2][:6] + "/" + f.split("/")[-1][:8], os.path.getsize(os.path.join(p, f))) for f in files(p) if f.endswith(".blob")]


for label, size, kw in (
    ("explicit dedicated_size_threshold=1MB", 3_000_000, dict(dedicated_size_threshold=1_000_000)),
    ("default thresholds, 20 MiB values", 20 * 1024 * 1024, {}),
):
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("blob", nullable=True, **kw)])
    p = fresh("a4b")
    vals = [payload(f"other{i}-", size) for i in range(2)]
    rows = pa.table({"id": [0, 1, 2], "blob": blob_array([vals[0], MARK + payload("subj-", size), vals[1]])}, schema=schema)
    lance.write_dataset(rows, p, data_storage_version="2.2", enable_stable_row_ids=True)
    ds = lance.dataset(p)
    print(f"\n#### {label}: .blob:", blobs(p), "mark in:", [f.split("/")[-1][:8] for f in grep(p, MARK)])
    ds.delete("id = 1")
    ds = lance.dataset(p)
    print("   compaction:", ds.optimize.compact_files(materialize_deletions_threshold=0.0))
    ds = lance.dataset(p)
    print("   after compact .blob:", blobs(p))
    print("   cleanup:", cleanup(ds))
    ds = lance.dataset(p)
    print("   after cleanup .blob:", blobs(p), "mark in:", grep(p, MARK))
    print("   survivors:", [len(b.read()) for b in ds.take_blobs("blob", ids=[0, 2])])
