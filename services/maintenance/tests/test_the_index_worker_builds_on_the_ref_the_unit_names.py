"""The index worker builds on the REF the unit names, not always on main, and says so in its event.

[[LH-019]]. `maintenance/reindex` publishes and answers 202, so this worker is what actually opens
the dataset. Until `IndexWorkItem` carried a `branch`, the catalog door could only refuse one:
building main's index while the API reported the branch's is the wrong-but-plausible answer that row
exists to remove, and it is worse than a refusal because nothing downstream can tell.

[[LH-214]]. The run it emits names the commit the build made: the branch, that branch's version, and
the branch's identifier. A branch keeps its own version sequence, so the version of a reopened URI
(main's latest) names a different snapshot, and a recreated branch restarts its numbering.

A branch is NOT openable by path — it lives at `tree/{branch}/` with its own `_versions/` and no
`data/` (`lance_docs/file_format.md:2746-2761`) — so the ref is checked out of the dataset the URI
names, the same idiom the compaction pair uses.

THE CONTROL LEG IS WHAT MAKES THIS MEAN ANYTHING. A branch that holds the same rows as main would
let a worker ignore `branch` entirely and still pass, so the fixture DIVERGES them first and the
assertions are about a column that exists on one ref and not the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from maintenance.api import index_work
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import COMPACTION, CREATE_INDEX
from service_kit.lakehouse.work_items import SCALAR_INDEX, IndexWorkItem


class _Emitter:
    """Records what the worker asked to emit, with the emitter's whole signature."""

    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []

    async def emit_maintenance(
        self,
        *,
        table_id: str,
        namespace: str,
        operation: str = COMPACTION,
        version: int | None = None,
        branch: str | None = None,
        branch_identifier: str | None = None,
    ) -> None:
        self.emitted.append({"operation": operation, "version": version, "branch": branch, "branch_identifier": branch_identifier})

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str, operation: str = COMPACTION) -> None:
        return None


@pytest.fixture
def diverged(tmp_path: Path) -> str:
    """Main has `id` only; the branch `work` additionally has `extra`. One ref can index a column
    the other cannot, which is what lets the assertions below distinguish them."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(256), pa.int64())}), uri)
    dataset = lance.dataset(uri)
    dataset.create_branch("work")
    on_branch = lance.dataset(uri).checkout_version(("work", None))
    on_branch.add_columns({"extra": "CAST(id AS BIGINT)"})
    return uri


@pytest.mark.asyncio
async def test_the_worker_builds_on_the_branch_the_unit_names_and_emits_that_commit(diverged: str) -> None:
    emitter = _Emitter()
    item = IndexWorkItem(uri=diverged, table_id="ns$t", column="extra", kind=SCALAR_INDEX, index_type="BTREE", name="extra_idx", branch="work")
    settings = MaintenanceSettings.model_validate({"MAINTENANCE_INDEX_TOPIC": "idx", "MAINTENANCE_EXECUTE_WORK": True})

    result = await index_work.handle_index_unit({"data": item.model_dump()}, settings, emitter)

    assert result["status"] == "SUCCESS", result
    branch = lance.dataset(diverged).checkout_version(("work", None))
    assert "extra_idx" in {index.name for index in branch.describe_indices()}, "the index did not land on the branch"
    identifier = str(lance.dataset(diverged).branches.list()["work"]["branch_identifier"][-1][1])
    assert emitter.emitted == [{"operation": CREATE_INDEX, "version": branch.version, "branch": "work", "branch_identifier": identifier}]
