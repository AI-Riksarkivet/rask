import io
import os

import lance_namespace as ln
import pyarrow as pa
from lance.namespace import MergeInsertIntoTableRequest


def ipc(t: pa.Table) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, t.schema) as w:
        w.write_table(t)
    return sink.getvalue()


def ns_at(root):
    os.makedirs(root, exist_ok=True)
    return ln.connect("dir", {"root": root})


def merge(ns, name, src, *, by_source_delete=True, filt=None, update=True, insert=True):
    req = MergeInsertIntoTableRequest(
        id=[name],
        on="id",
        when_matched_update_all=update,
        when_not_matched_insert_all=insert,
        when_not_matched_by_source_delete=by_source_delete,
        when_not_matched_by_source_delete_filt=filt,
    )
    return ns.merge_insert_into_table(req, ipc(src))
