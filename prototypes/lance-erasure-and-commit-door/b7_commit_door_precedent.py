"""LH-330 (7): does the /compaction_commit shape (worker writes files, a coordinator commits metadata only) carry each
transaction type the writers use? Prototype door: worker builds an uncommitted operation, it crosses a process boundary
as bytes (pickle, standing in for the wire), and the door commits it with LanceDataset.commit at the worker's read_version."""

import os
import pickle
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lance.fragment as F
from lance.dataset import LanceOperation

from common import *


def door(uri, wire: bytes):
    read_version, op = pickle.loads(wire)
    ds = lance.LanceDataset.commit(uri, op, read_version=read_version)
    return ds


def base(name):
    p = fresh(f"b7_{name}")
    create(p, pa.table({"id": pa.array(range(20), pa.int64()), "payload": pa.array([f"p{i}" for i in range(20)])}), max_rows_per_file=10)
    return p, lance.dataset(p)


def attempt(name, build):
    p, ds = base(name)
    try:
        rv, op = build(p, ds)
        try:
            wire = pickle.dumps((rv, op))
        except Exception as e:
            print(f"{name:28s} SERIALIZE FAILED (pickle): {type(e).__name__}: {str(e)[:150]}")
            return None
        out = door(p, wire)
        t = out.read_transaction(out.version)
        print(f"{name:28s} OK  v{rv}->v{out.version}  op={type(t.operation).__name__}  rows={out.count_rows()}  wire={len(wire)}B")
        return p
    except Exception as e:
        print(f"{name:28s} FAILED {type(e).__name__}: {str(e)[:220]}")


# Append (the existing /commit)
attempt(
    "Append(write_fragments)",
    lambda p, ds: (ds.version, LanceOperation.Append(F.write_fragments(pa.table({"id": pa.array([100], pa.int64()), "payload": ["x"]}), p))),
)


# Overwrite (create/re-seed with mode=overwrite, the /ingest-media and Ray-lane empty-overwrite shape)
def ow(p, ds):
    t = pa.table({"id": pa.array([1, 2], pa.int64()), "payload": ["a", "b"]})
    return ds.version, LanceOperation.Overwrite(t.schema, F.write_fragments(t, p, mode="overwrite"))


attempt("Overwrite(write_fragments)", ow)


# Update via merge_insert.execute_uncommitted (the full-sync re-seed / stage merge)
def mi(p, ds):
    src = pa.table({"id": pa.array([1, 2, 50], pa.int64()), "payload": ["u1", "u2", "new"]})
    res = ds.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().when_not_matched_by_source_delete().execute_uncommitted(src)
    txn = res[0] if isinstance(res, tuple) else res["transaction"] if isinstance(res, dict) else res
    return txn.read_version, txn.operation


attempt("Update(merge_insert uncommit)", mi)


# Merge (add a data-carrying column per fragment: worker merge_columns, coordinator commits Merge)
def mg(p, ds):
    frags, schema = [], None
    for fr in ds.get_fragments():
        md, schema = fr.merge_columns(lambda b: pa.record_batch([pa.array([f"e{i}" for i in b["id"].to_pylist()])], names=["emb"]), columns=["id"])
        frags.append(md)
    return ds.version, LanceOperation.Merge(frags, schema)


attempt("Merge(merge_columns)", mg)


# Delete (worker computes new deletion files per fragment)
def dl(p, ds):
    upd, gone = [], []
    for fr in ds.get_fragments():
        md = fr.delete("id IN (3, 4)")
        if md is None:
            gone.append(fr.fragment_id)
        elif md.deletion_file is not None and (fr.metadata.deletion_file is None or md.deletion_file != fr.metadata.deletion_file):
            upd.append(md)
    return ds.version, LanceOperation.Delete(upd, gone, "id IN (3, 4)")


attempt("Delete(fragment.delete)", dl)
# UpdateConfig carrying schema metadata (the dataset-id stamp, update_schema_metadata)
attempt(
    "UpdateConfig(schema meta)",
    lambda p, ds: (ds.version, LanceOperation.UpdateConfig(schema_metadata_updates=LanceOperation.UpdateMap({"rask.dataset_id": "abc"}, replace=False))),
)


# CreateIndex (worker builds the index files uncommitted; coordinator commits)
def _fts(d):
    try:
        return f"FTS rows={d.to_table(full_text_query='p3').num_rows}"
    except Exception as e:
        return f"FTS QUERY FAILS AFTER COMMIT: {str(e)[:160]}"


for itype, col in (("BTREE", "id"), ("INVERTED", "payload")):

    def ci(p, ds, itype=itype, col=col):
        idx = ds.create_index_uncommitted(col, itype, name=f"{col}_idx", fragment_ids=[f.fragment_id for f in ds.get_fragments()])
        return ds.version, LanceOperation.CreateIndex(new_indices=[idx], removed_indices=[])

    q = attempt(f"CreateIndex({itype} uncommit)", ci)
    if q:
        d = lance.dataset(q)
        print(
            f"{'':28s} indices={[i['name'] for i in d.list_indices()]} query uses it: {'ScalarIndexQuery' in d.scanner(filter='id = 3').explain_plan() if itype == 'BTREE' else _fts(d)}"
        )
# Distributed FTS the documented way: worker segment(s), then commit_existing_index_segments (guide.md:1410-1430)
for how in ("commit_existing", "merge_then_commit"):
    p, ds = base(f"fts_{how}")
    frs = [f.fragment_id for f in ds.get_fragments()]
    segs = [ds.create_index_uncommitted("payload", "INVERTED", name="payload_idx", fragment_ids=[fid]) for fid in frs]
    wire = pickle.dumps(segs)
    before = set(files(p))
    try:
        segs2 = pickle.loads(wire)
        if how == "merge_then_commit":
            segs2 = [ds.merge_existing_index_segments(segs2)]
        out = ds.commit_existing_index_segments("payload_idx", "payload", segs2)
        written = sorted(set(files(p)) - before)
        print(
            f"FTS {how:20s} OK v{out.version} op={type(out.read_transaction(out.version).operation).__name__} {_fts(lance.dataset(p))}; files the door wrote: {len([w for w in written if w.startswith('_indices')])} index files"
        )
    except Exception as e:
        print(f"FTS {how:20s} FAILED {type(e).__name__}: {str(e)[:200]}")


# Rewrite (the existing /compaction_commit precedent, for reference)
def rw(p, ds):
    from lance.optimize import Compaction, RewriteResult

    plan = Compaction.plan(ds, {"target_rows_per_fragment": 1000})
    results = [RewriteResult.from_json(t.execute(ds).to_json()) for t in plan.tasks]
    groups = [LanceOperation.RewriteGroup(old_fragments=[ds.get_fragment(f.fragment_id).metadata for f in []], new_fragments=[]) for _ in []]
    return plan.read_version, ("compaction", results)


p, ds = base("rewrite")
from lance.optimize import Compaction, RewriteResult


plan = Compaction.plan(ds, {"target_rows_per_fragment": 1000})
wire = [t.execute(ds).json() for t in plan.tasks]
m = Compaction.commit(ds, [RewriteResult.from_json(w) for w in wire])
ds = lance.dataset(p)
print(f"{'Rewrite(compaction commit)':28s} OK  ->v{ds.version} op={type(ds.read_transaction(ds.version).operation).__name__} metrics={m}")

# Does a door-committed Update rebase over a concurrent append, as the in-process call does?
p, ds = base("update_race")
src = pa.table({"id": pa.array([1], pa.int64()), "payload": ["u1"]})
res = ds.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute_uncommitted(src)
txn = res[0] if isinstance(res, tuple) else res["transaction"] if isinstance(res, dict) else res
lance.write_dataset(pa.table({"id": pa.array([999], pa.int64()), "payload": ["concurrent"]}), p, mode="append")
try:
    out = door(p, pickle.dumps((txn.read_version, txn.operation)))
    print("Update built at v%d committed after a concurrent append: OK v%d rows=%d" % (txn.read_version, out.version, out.count_rows()))
except Exception as e:
    print("Update after concurrent append: FAILED", str(e)[:200])
# Full-sync Update with by-source delete built before a concurrent append: does the door's commit delete the concurrent row?
p, ds = base("fullsync_race")
src = pa.table({"id": pa.array(range(20), pa.int64()), "payload": [f"q{i}" for i in range(20)]})
res = ds.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().when_not_matched_by_source_delete().execute_uncommitted(src)
txn = res[0] if isinstance(res, tuple) else res["transaction"] if isinstance(res, dict) else res
lance.write_dataset(pa.table({"id": pa.array([999], pa.int64()), "payload": ["concurrent"]}), p, mode="append")
try:
    out = door(p, pickle.dumps((txn.read_version, txn.operation)))
    print(
        "full-sync Update after concurrent append: OK v%d rows=%d, id 999 present=%s"
        % (out.version, out.count_rows(), out.to_table(filter="id = 999").num_rows == 1)
    )
except Exception as e:
    print("full-sync Update after concurrent append: FAILED", str(e)[:200])
