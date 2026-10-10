"""Which files the coordinator writes when finalizing worker-built FTS segments (commit_existing_index_segments)."""

import os
import pickle
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *


p = fresh("b7b")
create(p, pa.table({"id": pa.array(range(20), pa.int64()), "payload": pa.array([f"p{i}" for i in range(20)])}), max_rows_per_file=10)
ds = lance.dataset(p)
segs = [ds.create_index_uncommitted("payload", "INVERTED", name="payload_idx", fragment_ids=[f.fragment_id]) for f in ds.get_fragments()]
w = set(files(p))
print("worker-written:", sorted(f for f in w if f.startswith("_indices")))
ds.commit_existing_index_segments("payload_idx", "payload", pickle.loads(pickle.dumps(segs)))
print("coordinator-written:", sorted(set(files(p)) - w))
