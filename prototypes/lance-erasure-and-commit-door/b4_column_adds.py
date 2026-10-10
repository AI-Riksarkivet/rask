"""LH-330 (4): merge_insert with a source column the target lacks; add_columns(NULL expr) + merge_insert to land a data-carrying column."""

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lance.namespace import AlterTableAddColumnsRequest
from lance_namespace_urllib3_client.models import AddColumnsEntry as NewColumnTransform

from common import *
from nsutil import *


def base():
    return pa.table({"id": pa.array(range(10), pa.int64()), "payload": pa.array([f"p{i}" for i in range(10)])})


def with_emb(ids):
    return pa.table({"id": pa.array(ids, pa.int64()), "payload": pa.array([f"p{i}" for i in ids]), "emb": pa.array([f"e{i}" for i in ids])})


root = fresh("b4")
ns = ns_at(root)
# (a) merge_insert whose source carries a column the target lacks
for via in ("native-ns", "pylance"):
    p = os.path.join(root, f"a_{via}.lance")
    create(p, base(), max_rows_per_file=5)
    try:
        if via == "native-ns":
            r = merge(ns, f"a_{via}", with_emb(list(range(10))), by_source_delete=False)
            print(f"(a) {via}: accepted -> version {r.version}")
        else:
            lance.dataset(p).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(with_emb(list(range(10))))
            print(f"(a) {via}: accepted")
        print("    schema now:", lance.dataset(p).schema.names)
    except Exception as e:
        print(f"(a) {via}: REFUSED {type(e).__name__}: {str(e)[:220]}")

# (b) /add_columns with a NULL expression, then /merge_insert carrying the column
p = os.path.join(root, "b.lance")
create(p, base(), max_rows_per_file=5)
v0 = lance.dataset(p).version
nulls = ["CAST(NULL AS STRING)", "NULL"]
for expr in nulls:
    try:
        try:
            r = ns.alter_table_add_columns(AlterTableAddColumnsRequest(id=["b"], new_columns=[NewColumnTransform(name="emb", expression=expr)]))
            print(f"(b) native-ns add_columns {expr!r}: OK -> version {r.version}; emb type:", lance.dataset(p).schema.field("emb").type)
            break
        except Exception as e0:
            print(f"(b) native-ns add_columns {expr!r}: {type(e0).__name__}: {str(e0)[:160]}  (the catalog door uses pylance add_columns, dataplane.py:2245)")
        lance.dataset(p).add_columns({"emb": expr})
        print(f"(b) pylance add_columns({{'emb': {expr!r}}}) [the door's call]: OK; emb type:", lance.dataset(p).schema.field("emb").type)
        break
    except Exception as e:
        print(f"(b) native add_columns expression {expr!r}: FAILED {type(e).__name__}: {str(e)[:200]}")
        try:
            lance.dataset(p).add_columns({"emb": expr})
            print(f"    pylance add_columns({expr!r}) OK, emb type:", lance.dataset(p).schema.field("emb").type)
            break
        except Exception as e2:
            print(f"    pylance add_columns({expr!r}) FAILED {str(e2)[:200]}")
ds = lance.dataset(p)
print("    after add: version", ds.version, "data files per fragment:", [len(f.data_files()) for f in ds.get_fragments()])
r = merge(ns, "b", with_emb(list(range(10))), by_source_delete=True)
ds = lance.dataset(p)
print(f"(b) merge_insert full rows+emb: version {r.version}, upd={r.num_updated_rows}; emb values:", ds.to_table(columns=["emb"])["emb"].to_pylist()[:4], "...")
print("    commits for add+merge:", ds.version - v0, [type(ds.read_transaction(v).operation).__name__ for v in range(v0 + 1, ds.version + 1)])

# (c) partial-column source (id + emb only) into a table that already has emb
p = os.path.join(root, "c.lance")
create(p, base(), max_rows_per_file=5)
lance.dataset(p).add_columns({"emb": "CAST(NULL AS STRING)"})
part = pa.table({"id": pa.array(range(10), pa.int64()), "emb": pa.array([f"e{i}" for i in range(10)])})
for via in ("native-ns", "pylance"):
    try:
        if via == "native-ns":
            r = merge(ns, "c", part, by_source_delete=False, insert=False)
            print(f"(c) {via} partial-column merge: OK v{r.version} upd={r.num_updated_rows}")
        else:
            lance.dataset(p).merge_insert("id").when_matched_update_all().execute(part)
            print(f"(c) {via} partial-column merge: OK")
        t = lance.dataset(p).to_table().sort_by("id")
        print("    row 3:", t.slice(3, 1).to_pylist())
    except Exception as e:
        print(f"(c) {via} partial-column merge: REFUSED {type(e).__name__}: {str(e)[:220]}")

# (d) the in-process lane's shape: pylance add_columns with data (one commit), for comparison
p = os.path.join(root, "d.lance")
create(p, base(), max_rows_per_file=5)
v0 = lance.dataset(p).version
ds = lance.dataset(p)


def udf(batch):
    return pa.record_batch([pa.array([f"e{i}" for i in batch["id"].to_pylist()])], names=["emb"])


ds.add_columns(udf, read_columns=["id"])
ds = lance.dataset(p)
print(
    "(d) pylance add_columns(UDF) commits:",
    ds.version - v0,
    [type(ds.read_transaction(v).operation).__name__ for v in range(v0 + 1, ds.version + 1)],
    "| data files per fragment:",
    [len(f.data_files()) for f in ds.get_fragments()],
)
