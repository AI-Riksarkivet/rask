import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lance.file import LanceFileReader

from common import *


N = b"zqxsubjectzqx"
SUBJECT_ROWID = None


def decoded_hits(p):
    """Read every index .lance file through LanceFileReader and look for the token/value in decoded columns."""
    hits = []
    for f in files(p):
        if not (f.startswith("_indices/") and f.endswith(".lance")):
            continue
        try:
            t = LanceFileReader(os.path.join(p, f)).read_all().to_table()
        except Exception as e:
            hits.append(f"{f}:UNREADABLE({str(e)[:60]})")
            continue
        if f.endswith("_tokens.lance"):
            hits.append(f"{f}[fst tokens={t.column('_token_next_id')[0].as_py()}]")
        if f.endswith("_docs.lance"):
            if SUBJECT_ROWID in t.column("_rowid").to_pylist():
                hits.append(f"{f}[_rowid {SUBJECT_ROWID} present, docs={t.num_rows}]")
            else:
                hits.append(f"{f}[_rowid absent, docs={t.num_rows}]")
        for c in t.column_names:
            col = t.column(c)
            if pa.types.is_string(col.type) or pa.types.is_large_string(col.type):
                if any(v and "zqxsubjectzqx" in v for v in col.to_pylist()):
                    hits.append(f"{f}[{c}]")
    return hits


def build(name):
    p = fresh(name)
    ids = list(range(200))
    nm = [f"person{i} lives here" for i in ids]
    nm[57] = "zqxsubjectzqx lives here"
    create(p, tbl(ids, nm), max_rows_per_file=50)
    ds = lance.dataset(p)
    global SUBJECT_ROWID
    SUBJECT_ROWID = ds.to_table(filter="id = 57", with_row_id=True)["_rowid"][0].as_py()
    ds.create_scalar_index("name", "BTREE", name="name_btree")
    ds = lance.dataset(p)
    ds.create_scalar_index("name", "INVERTED", name="name_fts")
    return p, lance.dataset(p)


def report(p, ds, label):
    idx_dirs = sorted({f.split("/")[1][:8] for f in files(p) if f.startswith("_indices/")})
    print(f"[{label}] v{ds.version} indices={[(i['name'], i['uuid'][:8]) for i in ds.list_indices()]} on-disk={idx_dirs}")
    print(f"   raw-byte hits: {grep(p, N)}")
    print(f"   decoded index hits: {decoded_hits(p)}")
    flt = "name = 'zqxsubjectzqx lives here'"
    fts = ds.to_table(full_text_query="zqxsubjectzqx").num_rows if any(i["name"] == "name_fts" for i in ds.list_indices()) else "n/a"
    print(f"   FTS hits={fts} btree hits={ds.to_table(filter=flt).num_rows}")


p, ds = build("a2_compact")
report(p, ds, "built")
ds.delete("id = 57")
ds = lance.dataset(p)
report(p, ds, "after delete")
try:
    ds.optimize.compact_files(defer_index_remap=True)
except Exception as e:
    print("   compact(defer_index_remap=True):", str(e)[:200])
m = ds.optimize.compact_files(materialize_deletions_threshold=0.0)
ds = lance.dataset(p)
print("   compaction:", m, "txn ops v5,v6:", [type(ds.read_transaction(v).operation).__name__ for v in (5, 6)])
report(p, ds, "after compact")
print("   cleanup:", cleanup(ds))
ds = lance.dataset(p)
report(p, ds, "after compact+cleanup")
ds.optimize.optimize_indices()
ds = lance.dataset(p)
report(p, ds, "after optimize_indices")
ds.create_scalar_index("name", "BTREE", name="name_btree", replace=True)
ds = lance.dataset(p)
ds.create_scalar_index("name", "INVERTED", name="name_fts", replace=True)
ds = lance.dataset(p)
report(p, ds, "after create(replace=True)")
print("   cleanup:", cleanup(ds))
ds = lance.dataset(p)
report(p, ds, "after replace+cleanup")

print("\n######## no compaction: replace index while the row is only masked by a deletion file")
p, ds = build("a2_nocompact")
ds.delete("id = 57")
ds = lance.dataset(p)
ds.create_scalar_index("name", "BTREE", name="name_btree", replace=True)
ds = lance.dataset(p)
ds.create_scalar_index("name", "INVERTED", name="name_fts", replace=True)
ds = lance.dataset(p)
print("   cleanup:", cleanup(ds))
ds = lance.dataset(p)
report(p, ds, "replace+cleanup, no compaction")

print("\n######## drop index then cleanup")
p, ds = build("a2_drop")
ds.delete("id = 57")
ds = lance.dataset(p)
ds.drop_index("name_btree")
ds = lance.dataset(p)
ds.drop_index("name_fts")
ds = lance.dataset(p)
report(p, ds, "after drop")
print("   cleanup:", cleanup(ds))
ds = lance.dataset(p)
report(p, ds, "after drop+cleanup")
