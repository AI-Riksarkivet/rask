import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *


def names(d):
    return sorted(d.to_table()["name"].to_pylist())


p = fresh("a5")
create(p, tbl([1, 2, 3], ["alice", "SUBJECT", "carol"]))
ds = lance.dataset(p)
pre = ds.version
ds.delete("name = 'SUBJECT'")
ds = lance.dataset(p)
print("head after delete v", ds.version, names(ds))
old = ds.checkout_version(pre)
old.restore()
ds = lance.dataset(p)
print("restore(pre) -> new head v", ds.version, names(ds), "txn op:", type(ds.read_transaction(ds.version).operation).__name__)
# erase again, compact, cleanup, then try restore
ds.delete("name = 'SUBJECT'")
ds = lance.dataset(p)
ds.optimize.compact_files(materialize_deletions_threshold=0.0)
ds = lance.dataset(p)
print("versions before cleanup:", [v["version"] for v in ds.versions()])
print("cleanup:", cleanup(ds))
ds = lance.dataset(p)
print("versions after cleanup:", [v["version"] for v in ds.versions()])
for v in (pre, pre + 1, pre + 2):
    try:
        ds.checkout_version(v).restore()
        print(f"restore v{v}: OK ->", names(lance.dataset(p)))
    except Exception as e:
        print(f"restore v{v}: FAILED", str(e)[:110])
print("SUBJECT bytes on disk:", grep(p, b"SUBJECT"))

print("\n== with compaction but WITHOUT cleanup: does restore bring it back?")
p = fresh("a5b")
create(p, tbl([1, 2, 3], ["alice", "SUBJECT", "carol"]))
ds = lance.dataset(p)
pre = ds.version
ds.delete("name = 'SUBJECT'")
ds = lance.dataset(p)
ds.optimize.compact_files(materialize_deletions_threshold=0.0)
ds = lance.dataset(p)
ds.checkout_version(pre).restore()
print("restore after compact (no cleanup):", names(lance.dataset(p)))
