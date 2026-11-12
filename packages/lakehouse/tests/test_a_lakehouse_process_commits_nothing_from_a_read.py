"""A lakehouse process commits nothing to a table from a read (`lakehouse.lance_app.build_lance_service_app`).

Lance migrates a dataset whose index segments overlap by a commit from whichever read notices: measured on pylance
13.0.0, two segments covering one fragment commit, and a read of the index's statistics then committed 472 migrations
before the process crashed. Every lakehouse process boots through the app factory, which forbids it. The read runs in a
process of its own that boots the factory as a service does, so a crash is an exit status the test reads rather than the
end of the test run, and its environment holds no switch the test run's own processes may have set.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import lance
import pyarrow as pa


#: A service's boot, then the read.
_READ = """
import logging, sys
from contextlib import asynccontextmanager

import lance

from lakehouse.lance_app import build_lance_service_app
from lakehouse.schemas.health import Readiness, ReadinessStatus


@asynccontextmanager
async def _lifespan(_app):
    yield


async def _ready(_request):
    return Readiness(status=ReadinessStatus.ready)


build_lance_service_app(
    title="t",
    docs_enabled=False,
    audit_enabled=False,
    lifespan=_lifespan,
    log=logging.getLogger("t"),
    ready_check=_ready,
    max_body_bytes=1024,
    request_deadline_seconds=30.0,
)
try:
    lance.dataset(sys.argv[1]).stats.index_stats("id_idx")
except OSError:
    pass
print(lance.dataset(sys.argv[1]).version)
"""


def test_a_read_of_an_index_whose_segments_overlap_commits_nothing(tmp_path: Path) -> None:
    root = str(tmp_path / "t")
    lance.write_dataset(pa.table({"id": pa.array(range(100), pa.int64())}), root, data_storage_version="2.2", enable_stable_row_ids=True)
    dataset = lance.dataset(root)
    overlapping = [dataset.create_index_uncommitted("id", index_type="BTREE", name="id_idx", fragment_ids=[0]) for _ in range(2)]
    operation = lance.LanceOperation.CreateIndex(new_indices=overlapping, removed_indices=[])
    committed = lance.LanceDataset.commit(root, operation, read_version=dataset.version).version
    environment = {name: value for name, value in os.environ.items() if name != "LANCE_AUTO_MIGRATION"}

    read = subprocess.run([sys.executable, "-c", _READ, root], capture_output=True, text=True, timeout=120, env=environment, check=False)

    assert (read.returncode, read.stdout.strip()) == (0, str(committed)), read.stderr[-2000:]
