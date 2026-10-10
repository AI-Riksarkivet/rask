import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *


def names(d):
    return sorted(d.to_table()["name"].to_pylist())


p = fresh("a6")
create(p, tbl([1], ["alice"]))
ds = lance.dataset(p)
X = ds.create_branch("X", reference=(None, 1))
lance.write_dataset(tbl([2], ["SUBJECT"]), X, mode="append")
ds = lance.dataset(p)
x = ds.checkout_version(("X", None))
xv = x.version
print("X head v", xv, names(x))
ds.tags.create("t1", ("X", xv))
print("tags:", ds.tags.list())
print("tag file:", open(os.path.join(p, "_refs/tags/t1.json")).read())
x.delete("name = 'SUBJECT'")
x = lance.dataset(p).checkout_version(("X", None))
x.optimize.compact_files(materialize_deletions_threshold=0.0)
x = lance.dataset(p).checkout_version(("X", None))
lance.write_dataset(tbl([3], ["later"]), x, mode="append")
x = lance.dataset(p).checkout_version(("X", None))
print("X head v", x.version, names(x), "X versions:", [v["version"] for v in x.versions()])
try:
    print("cleanup X (default error_if_tagged_old_versions=True):", x.cleanup_old_versions(older_than=timedelta(0), delete_unverified=True))
except Exception as e:
    print("cleanup X default refused:", str(e)[:200])
x = lance.dataset(p).checkout_version(("X", None))
print("cleanup X (error_if_tagged_old_versions=False):", cleanup(x))
x = lance.dataset(p).checkout_version(("X", None))
print("X versions after cleanup:", [v["version"] for v in x.versions()])
ds = lance.dataset(p)
print("checkout tag t1:", names(ds.checkout_version("t1")))
print("SUBJECT on disk:", grep(p, b"SUBJECT"))
print("cleanup main:", cleanup(ds))
ds = lance.dataset(p)
print("after main cleanup, tag t1:", names(ds.checkout_version("t1")), "SUBJECT on disk:", grep(p, b"SUBJECT"))
ds.tags.delete("t1")
ds = lance.dataset(p)
x = ds.checkout_version(("X", None))
print("after tag delete, cleanup X:", cleanup(x))
print("SUBJECT on disk:", grep(p, b"SUBJECT"))
print("files:", files(p))
