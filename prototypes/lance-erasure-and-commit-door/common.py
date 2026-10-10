import os
import shutil
import tempfile
from datetime import timedelta

import lance
import pyarrow as pa


# Scratch tables go outside the repo; override with LANCE_PROTO_DATA.
ROOT = os.environ.get("LANCE_PROTO_DATA", os.path.join(tempfile.gettempdir(), "lance-proto"))
print("lance", lance.__version__)


def fresh(name):
    p = os.path.join(ROOT, name)
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def files(p):
    out = []
    for d, _, fs in os.walk(p):
        for f in fs:
            out.append(os.path.relpath(os.path.join(d, f), p))
    return sorted(out)


def grep(p, needle: bytes):
    hits = []
    for f in files(p):
        with open(os.path.join(p, f), "rb") as fh:
            if needle in fh.read():
                hits.append(f)
    return hits


def tbl(ids, names):
    return pa.table({"id": pa.array(ids, pa.int64()), "name": pa.array(names, pa.string())})


def create(p, t, **kw):
    return lance.write_dataset(t, p, data_storage_version="2.2", enable_stable_row_ids=True, **kw)


def cleanup(ds):
    return ds.cleanup_old_versions(older_than=timedelta(0), delete_unverified=True, error_if_tagged_old_versions=False)
