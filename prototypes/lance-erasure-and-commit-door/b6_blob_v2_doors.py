"""LH-330 (6): blob-v2 rows as Arrow-IPC bodies through native insert/merge_insert, and as client-written fragments through the /commit function."""

import json
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lance import blob_array, blob_field
from lance.namespace import InsertIntoTableRequest

from common import *
from nsutil import *


schema = pa.schema([pa.field("id", pa.int64()), blob_field("blob", nullable=True)])


def rows(ids, size):
    return pa.table({"id": pa.array(ids, pa.int64()), "blob": blob_array([bytes([65 + i % 26]) * size for i in ids])}, schema=schema)


root = fresh("b6")
ns = ns_at(root)
for label, size in (("inline", 1000), ("packed", 100_000), ("dedicated", 20 * 1024 * 1024)):
    p = os.path.join(root, f"{label}.lance")
    lance.write_dataset(rows([0, 1], size), p, data_storage_version="2.2", enable_stable_row_ids=True)
    body = rows([2, 3], size)
    b = ipc(body)
    back = pa.ipc.open_stream(b).read_all()
    print(
        f"\n### {label} ({size} B): IPC body {len(b)} B; blob field after IPC round trip: {back.schema.field('blob').type} meta={back.schema.field('blob').metadata}"
    )
    try:
        r = ns.insert_into_table(InsertIntoTableRequest(id=[label], mode="append"), b)
        ds = lance.dataset(p)
        print(f"   native insert_into_table: OK v{r.version}; rows={ds.count_rows()}; blob sizes={[len(x.read()) for x in ds.take_blobs('blob', ids=[2, 3])]}")
        print(f"   .blob files: {[f for f in files(p) if f.endswith('.blob')]}")
    except Exception as e:
        print(f"   native insert_into_table: FAILED {type(e).__name__}: {str(e)[:300]}")
    try:
        r = merge(ns, label, rows([3, 4], size), by_source_delete=False)
        ds = lance.dataset(p)
        print(
            f"   native merge_insert_into_table: OK v{r.version} upd={r.num_updated_rows} ins={r.num_inserted_rows}; rows={ds.count_rows()}; blob(id=4) size={len(ds.take_blobs('blob', ids=[ds.to_table(filter='id = 4', with_row_id=True)['_rowid'][0].as_py()])[0].read())}"
        )
    except Exception as e:
        print(f"   native merge_insert_into_table: FAILED {type(e).__name__}: {str(e)[:300]}")
    # client-written fragments, the /commit shape
    try:
        frags = lance.fragment.write_fragments(rows([10, 11], size), p)
        fj = [f.to_json() if isinstance(f.to_json(), dict) else json.loads(f.to_json()) for f in frags]
        df = fj[0]["files"][0]
        print(
            f"   write_fragments: {len(frags)} frag(s); data file version {df.get('file_major_version')}.{df.get('file_minor_version')} base_id={df.get('base_id')}; "
            f"new .blob under data/: {[f for f in files(p) if f.endswith('.blob')]}"
        )
        try:
            from catalog.services.dataplane import commit_appended_fragments

            v, n = commit_appended_fragments(p, {}, fj, lance.dataset(p).version)
            ds = lance.dataset(p)
            print(
                f"   catalog commit_appended_fragments (the /commit function, in-process): OK v{v} rows={n}; blob(id=10)={len(ds.take_blobs('blob', ids=[ds.to_table(filter='id = 10', with_row_id=True)['_rowid'][0].as_py()])[0].read())} B; data_storage_version={ds.data_storage_version}"
            )
        except Exception as e:
            print(f"   catalog commit_appended_fragments: REFUSED/FAILED {type(e).__name__}: {str(e)[:400]}")
    except Exception as e:
        print(f"   write_fragments FAILED {type(e).__name__}: {str(e)[:300]}")
