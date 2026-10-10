import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *


def setup(name):
    p = fresh(name)
    ds = create(p, tbl([1, 2, 3], ["alice", "SUBJECT", "carol"]))  # v1
    ds = lance.write_dataset(tbl([4], ["dave"]), p, mode="append")  # v2
    A = ds.create_branch("A", reference=(None, 1))
    lance.write_dataset(tbl([10], ["a-own"]), A, mode="append")
    ds = lance.dataset(p)
    B = ds.create_branch("B", reference=("A", None))
    lance.write_dataset(tbl([20], ["b-own"]), B, mode="append")
    lance.dataset(p).delete("name = 'SUBJECT'")
    return p, lance.dataset(p)


def names(d):
    return sorted(d.to_table()["name"].to_pylist())


print("== case 1: clean B's own old versions, then delete A")
p, ds = setup("a1b_case1")
b = ds.checkout_version(("B", None))
print("B version", b.version, "B versions", [v["version"] for v in b.versions()])
print("cleanup B:", cleanup(b))
ds = lance.dataset(p)
try:
    ds.branches.delete("A")
    print("delete A: OK")
except Exception as e:
    print("delete A refused:", e)

print("== case 2: delete B then A (bottom-up), recreate A and B on clean head, cleanup")
p, ds = setup("a1b_case2")
ds.branches.delete("B")
print("delete B OK")
print("tree/B files left:", [f for f in files(p) if f.startswith("tree/B")])
ds = lance.dataset(p)
ds.branches.delete("A")
print("delete A OK")
print("tree/A files left:", [f for f in files(p) if f.startswith("tree/A")])
ds = lance.dataset(p)
try:
    a = ds.create_branch("A", reference=(None, ds.version))
    print("recreate A at main", ds.version, "->", names(a))
    ds = lance.dataset(p)
    b = ds.create_branch("B", reference=("A", None))
    print("recreate B from A ->", names(b))
except Exception as e:
    print("recreate FAILED:", type(e).__name__, e)
ds = lance.dataset(p)
print("branches:", {k: (v["parent_branch"], v["parent_version"]) for k, v in ds.branches.list().items()})
print("cleanup main:", cleanup(ds))
ds = lance.dataset(p)
print("versions main:", [v["version"] for v in ds.versions()])
print("files containing SUBJECT:", grep(p, b"SUBJECT"))
for v in (1, 2):
    try:
        print(f"checkout main v{v}:", names(ds.checkout_version(v)))
    except Exception as e:
        print(f"checkout main v{v} FAILED:", str(e)[:120])

print("== case 3: delete A first while B exists -> refused (repeat), delete A with B's head rebuilt? try branch metadata only")
p, ds = setup("a1b_case3")
try:
    ds.branches.delete("A")
except Exception as e:
    print("refused:", str(e)[:140])
