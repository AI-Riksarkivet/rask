"""LH-330 (3) splitting a full-sync merge_insert and (5) _rowid across a by-source-delete re-seed, via the native DirectoryNamespace."""

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from nsutil import *


def seed():
    ids = list(range(100))
    return pa.table({"id": pa.array(ids, pa.int64()), "payload": pa.array([f"v1-{i}" for i in ids])})


def reseed_source():
    # full sync: drop ids 10 and 60, change payload of every id % 7 == 0, add 100..109
    ids = [i for i in range(110) if i not in (10, 60)]
    pl = [f"v2-{i}" if (i % 7 == 0 or i >= 100) else f"v1-{i}" for i in ids]
    return pa.table({"id": pa.array(ids, pa.int64()), "payload": pa.array(pl)})


def state(p):
    ds = lance.dataset(p)
    t = ds.to_table(columns=["id", "payload"], with_row_id=True).sort_by("id")
    return ds, {r["id"]: (r["payload"], r["_rowid"]) for r in t.to_pylist()}


def run(label, splits):
    root = fresh(f"b3_{label}")
    ns = ns_at(root)
    p = os.path.join(root, "t.lance")
    create(p, seed(), max_rows_per_file=25)
    v0, before = state(p)
    v0 = v0.version
    src = reseed_source()
    results = []
    for lo, hi in splits:
        part = src.filter((pa.compute.field("id") >= lo) & (pa.compute.field("id") < hi))
        filt = None if (lo, hi) == (-1, 10**9) else f"id >= {lo} AND id < {hi}"
        r = merge(ns, "t", part, filt=filt)
        results.append((r.version, r.num_updated_rows, r.num_inserted_rows, r.num_deleted_rows))
    ds, after = state(p)
    print(f"[{label}] per-request (version, upd, ins, del): {results}; versions committed: {ds.version - v0}")
    print(f"   txn ops: {[type(ds.read_transaction(v).operation).__name__ for v in range(v0 + 1, ds.version + 1)]}")
    return before, after


b, unsplit = run("unsplit", [(-1, 10**9)])
_, split = run("split3", [(0, 40), (40, 80), (80, 10**9)])
same_rows = {k: v[0] for k, v in unsplit.items()} == {k: v[0] for k, v in split.items()}
same_rowid = {k: v[1] for k, v in unsplit.items()} == {k: v[1] for k, v in split.items()}
print("split rows == unsplit rows:", same_rows, "| split _rowid == unsplit _rowid:", same_rowid, "| row counts", len(unsplit), len(split))
if not same_rowid:
    diff = [(k, unsplit[k][1], split[k][1]) for k in unsplit if unsplit[k][1] != split[k][1]][:8]
    print("   first _rowid differences (id, unsplit, split):", diff)

# what happens to rows a split request does NOT cover if the filter is omitted (the hazard the filter guards)
root = fresh("b3_unconfined")
ns = ns_at(root)
p = os.path.join(root, "t.lance")
create(p, seed(), max_rows_per_file=25)
part = reseed_source().filter(pa.compute.field("id") < 40)
r = merge(ns, "t", part, filt=None)
print("[unconfined split request, ids<40 only] deleted:", r.num_deleted_rows, "rows left:", lance.dataset(p).count_rows())

# (5) row identity: compare _rowid before/after the unsplit re-seed for kept ids
kept_unchanged = [k for k in b if k in unsplit and b[k][0] == unsplit[k][0]]
kept_updated = [k for k in b if k in unsplit and b[k][0] != unsplit[k][0]]
print("\n(5) _rowid kept for unchanged matched rows:", all(b[k][1] == unsplit[k][1] for k in kept_unchanged), f"({len(kept_unchanged)} rows)")
print(
    "    _rowid kept for UPDATED matched rows:",
    all(b[k][1] == unsplit[k][1] for k in kept_updated),
    f"({len(kept_updated)} rows)",
    "sample:",
    [(k, b[k][1], unsplit[k][1]) for k in kept_updated[:4]],
)
new_ids = [k for k in unsplit if k not in b]
print("    new rows' _rowid:", [unsplit[k][1] for k in new_ids[:5]], "| max old _rowid:", max(v[1] for v in b.values()))
print(
    "    has_stable_row_ids:",
    lance.dataset(os.path.join(fresh("b3_dummy"), "")) if False else lance.dataset(os.path.join(ROOT, "b3_unsplit", "t.lance")).has_stable_row_ids,
)
