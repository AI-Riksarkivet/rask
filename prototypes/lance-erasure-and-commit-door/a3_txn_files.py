import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *


PRED = b"zqxsubjectzqx"
p = fresh("a3")
create(p, tbl(list(range(100)), [f"n{i}" for i in range(99)] + ["zqxsubjectzqx"]), max_rows_per_file=50)
ds = lance.dataset(p)
ds.delete("name = 'zqxsubjectzqx'")
ds = lance.dataset(p)
print("delete txn predicate:", ds.read_transaction(ds.version).operation.predicate)
print("[after delete] files with predicate:", grep(p, PRED))
print("[after delete] .txn files:", [f for f in files(p) if f.endswith(".txn")])
print("cleanup (delete is head):", cleanup(ds))
ds = lance.dataset(p)
print("   files with predicate:", grep(p, PRED))
ds.optimize.compact_files(materialize_deletions_threshold=0.0)
ds = lance.dataset(p)
print("[after compact] v", ds.version, "files with predicate:", grep(p, PRED))
print("cleanup:", cleanup(ds))
ds = lance.dataset(p)
print("[after compact+cleanup] files with predicate:", grep(p, PRED))
print("   remaining files:", files(p))
print("   get_transactions:", [type(t.operation).__name__ if t else None for t in ds.get_transactions(10)])

print("\n== same but without compaction: one more append after the delete, then cleanup")
p = fresh("a3b")
create(p, tbl(list(range(100)), [f"n{i}" for i in range(99)] + ["zqxsubjectzqx"]))
ds = lance.dataset(p)
ds.delete("name = 'zqxsubjectzqx'")
lance.write_dataset(tbl([1000], ["later"]), p, mode="append")
ds = lance.dataset(p)
print("cleanup:", cleanup(ds))
ds = lance.dataset(p)
print("files with predicate:", grep(p, PRED))
print("files:", files(p))
