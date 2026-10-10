import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *


p = fresh("a1")
ds = create(p, tbl([1, 2, 3], ["alice", "SUBJECT", "carol"]))  # v1
ds = lance.write_dataset(tbl([4], ["dave"]), p, mode="append")  # v2
A = ds.create_branch("A", reference=(None, 1))
lance.write_dataset(tbl([10], ["a-own"]), A, mode="append")
ds = lance.dataset(p)
B = ds.create_branch("B", reference=("A", None))
lance.write_dataset(tbl([20], ["b-own"]), B, mode="append")
ds = lance.dataset(p)
print("branches:", ds.branches.list())
print("files before delete A:")
[print("  ", f) for f in files(p)]
try:
    ds.branches.delete("A")
    print("delete A: OK (no refusal)")
except Exception as e:
    print("delete A refused:", type(e).__name__, e)
ds = lance.dataset(p)
print("branches after:", ds.branches.list())
print("files after delete A:")
[print("  ", f) for f in files(p)]
try:
    b = ds.checkout_version(("B", None))
    print("B readable:", sorted(b.to_table()["name"].to_pylist()))
    print("B data files:", [df.path for fr in b.get_fragments() for df in fr.data_files()])
    print("B base_paths:", getattr(b, "_ds", None) and None)
except Exception as e:
    print("B read FAILED:", type(e).__name__, e)
try:
    b1 = ds.checkout_version(("B", 1))
    print("B v1 readable:", sorted(b1.to_table()["name"].to_pylist()))
except Exception as e:
    print("B v1 read FAILED:", type(e).__name__, e)
# erase subject on main, then recreate A at main's later version
ds.delete("name = 'SUBJECT'")
ds = lance.dataset(p)
print("main head", ds.version, sorted(ds.to_table()["name"].to_pylist()))
try:
    ds.create_branch("A", reference=(None, ds.version))
    print("recreate A at main head: OK")
except Exception as e:
    print("recreate A FAILED:", type(e).__name__, e)
ds = lance.dataset(p)
print("branches:", ds.branches.list())
try:
    b = ds.checkout_version(("B", None))
    print("B after A recreated:", sorted(b.to_table()["name"].to_pylist()))
except Exception as e:
    print("B read after recreate FAILED:", type(e).__name__, e)
a = ds.checkout_version(("A", None))
print("new A content:", sorted(a.to_table()["name"].to_pylist()))
# cleanup on main and on B, check B still readable
st = cleanup(ds)
print("cleanup main:", st)
ds = lance.dataset(p)
try:
    b = ds.checkout_version(("B", None))
    print("B after main cleanup:", sorted(b.to_table()["name"].to_pylist()))
except Exception as e:
    print("B after cleanup FAILED:", type(e).__name__, e)
print("files with SUBJECT:", grep(p, b"SUBJECT"))
print("final files:")
[print("  ", f) for f in files(p)]
