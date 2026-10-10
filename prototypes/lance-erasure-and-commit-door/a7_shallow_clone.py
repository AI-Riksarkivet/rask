import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *


def names(d):
    return sorted(d.to_table()["name"].to_pylist())


p = fresh("a7_src")
c = fresh("a7_clone")
create(p, tbl([1, 2, 3], ["alice", "SUBJECT", "carol"]))
ds = lance.dataset(p)
clone = ds.shallow_clone(c, ds.version)
print("clone:", names(lance.dataset(c)), "clone own files:", files(c))
ds.delete("name = 'SUBJECT'")
ds = lance.dataset(p)
ds.optimize.compact_files(materialize_deletions_threshold=0.0)
ds = lance.dataset(p)
print("source tags/branches:", ds.tags.list(), ds.branches.list())
print("cleanup source:", cleanup(ds))
ds = lance.dataset(p)
print("source versions:", [v["version"] for v in ds.versions()], "SUBJECT in source dir:", grep(p, b"SUBJECT"))
try:
    print("clone after source erasure+cleanup:", names(lance.dataset(c)))
except Exception as e:
    print("clone read FAILED:", str(e)[:200])
