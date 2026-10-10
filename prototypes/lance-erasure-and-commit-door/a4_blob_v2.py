import os
import shutil
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lance import DatasetBasePath, blob_array, blob_field

from common import *


MARK = b"ZQXSUBJECTBLOBZQX"


def payload(tag, size):
    return (tag.encode() * (size // len(tag) + 1))[:size]


def blobs(p):
    return [(f, os.path.getsize(os.path.join(p, f))) for f in files(p) if f.endswith(".blob")]


schema = pa.schema([pa.field("id", pa.int64()), blob_field("blob", nullable=True)])

for mode in ("inline", "packed", "dedicated"):
    p = fresh(f"a4_{mode}")
    size = {"packed": 100_000, "dedicated": 3_000_000, "inline": 1000}[mode]
    vals = [payload(f"other{i}-", size) for i in range(4)]
    rows = pa.table({"id": [0, 1, 2, 3, 4], "blob": blob_array(vals[:2] + [MARK + payload("subj-", size)] + vals[2:])}, schema=schema)
    lance.write_dataset(rows, p, data_storage_version="2.2", enable_stable_row_ids=True)
    ds = lance.dataset(p)
    print(f"\n#### {mode} ({size} B each): .blob files:", blobs(p), "| mark in:", grep(p, MARK))
    ds.delete("id = 2")
    ds = lance.dataset(p)
    print("   after delete (no compact) + cleanup:", cleanup(ds))
    ds = lance.dataset(p)
    print("   .blob:", blobs(p), "mark in:", grep(p, MARK))
    m = ds.optimize.compact_files(materialize_deletions_threshold=0.0)
    ds = lance.dataset(p)
    print("   compaction:", m)
    print("   after compact .blob:", blobs(p), "mark in:", grep(p, MARK))
    print("   cleanup:", cleanup(ds))
    ds = lance.dataset(p)
    print("   after cleanup .blob:", blobs(p), "mark in:", grep(p, MARK))
    print("   rows:", ds.count_rows(), "survivors readable:", [len(b.read()) for b in ds.take_blobs("blob", ids=[0, 1, 3, 4])])

EXT = os.path.join(ROOT, "ext_store")
for where in ("registered_base", "outside_bases"):
    p = fresh(f"a4_external_{where}")
    shutil.rmtree(EXT, ignore_errors=True)
    os.makedirs(EXT)
    ext_file = os.path.join(EXT, f"subject_{where}.bin")
    open(ext_file, "wb").write(MARK * 10)
    other = os.path.join(EXT, f"other_{where}.bin")
    open(other, "wb").write(b"x" * 100)
    rows = pa.table({"id": [1, 2], "blob": blob_array(["file://" + other, "file://" + ext_file])}, schema=schema)
    kw = dict(initial_bases=[DatasetBasePath("file://" + EXT, name="ext")]) if where == "registered_base" else dict(allow_external_blob_outside_bases=True)
    try:
        lance.write_dataset(rows, p, data_storage_version="2.2", enable_stable_row_ids=True, **kw)
    except Exception as e:
        print(f"\n#### external {where}: write FAILED", str(e)[:200])
        continue
    ds = lance.dataset(p)
    print(f"\n#### external {where}: subject readable:", ds.take_blobs("blob", ids=[1])[0].read()[:17])
    ds.delete("id = 2")
    ds = lance.dataset(p)
    ds.optimize.compact_files(materialize_deletions_threshold=0.0)
    ds = lance.dataset(p)
    print("   cleanup:", cleanup(ds))
    ds = lance.dataset(p)
    print("   external subject file still on disk:", os.path.exists(ext_file), "| other still:", os.path.exists(other))
    print("   dataset .blob/mark:", blobs(p), grep(p, MARK))
