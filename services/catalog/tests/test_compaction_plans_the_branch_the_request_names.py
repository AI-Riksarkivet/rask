"""A compaction planned for a branch must read the BRANCH's fragments, not main's.

[[LH-019]]. `plan_table_compaction` and `commit_table_compaction` declared `branch` only to refuse it,
with the reason written at the door: "planning against main and reporting it as the branch's work would
compact the wrong dataset with a 200". That refusal was right while the pair opened `lance.dataset(location)`
— which resolves MAIN whatever the request says.

THESE TWO DOORS ARE NOT GATED ON THE RECLAIM QUESTION, which is why they can open now while three of their
siblings cannot. Measured per door: `plan_table_compaction` and `commit_table_compaction` reference
`_base_refs` zero times and call `run_gc` never — they plan and commit a rewrite through pylance
in-process and reclaim nothing. `maintenance/preview`, `/run` and `/compact` DO touch `_base_refs`, so what
they may reclaim on a branch is [[LH-094]]'s open question and they stay refused.

A COMPACTION IS NOT A RECLAIM, and the distinction is the whole licence for this change. Compaction
rewrites fragments and mints a version; the old files stay until something reclaims them. So opening these
two doors to a branch changes which dataset is REWRITTEN, never what is DELETED — and the deletion
question is the one nobody has answered.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table, plan_compaction


lance = pytest.importorskip("lance")

TABLE_ID = ["rows"]
BRANCH = "work"


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _rows(n: int, start: int = 0) -> pa.Table:
    return pa.table({"id": pa.array(range(start, start + n), pa.int64())})


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    """Main with ONE fragment, the branch with several — so a plan can tell them apart.

    The asymmetry is the instrument: a planner reading main finds nothing worth merging, and one reading
    the branch finds work. Equal fragment counts would make both answers look alike.
    """
    namespace = lance_ns = __import__("lance_namespace").connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, _ipc(_rows(1)), mode="create")
    open_dataset(namespace, {}, TABLE_ID).create_branch(BRANCH, None)
    branch = open_dataset(namespace, {}, TABLE_ID, branch=BRANCH)
    for i in range(1, 5):  # four more single-row fragments, only on the branch
        branch.insert(_rows(1, start=i))
    return lance_ns


def _location(ns) -> str:  # noqa: ANN001
    return open_dataset(ns, {}, TABLE_ID).uri


def test_the_branch_really_has_more_fragments_than_main(ns) -> None:  # noqa: ANN001
    """Without this the assertion below could pass by planning either ref."""
    main_frags = len(open_dataset(ns, {}, TABLE_ID).get_fragments())
    branch_frags = len(open_dataset(ns, {}, TABLE_ID, branch=BRANCH).get_fragments())
    assert branch_frags > main_frags, f"the fixture did not diverge the refs (main={main_frags}, branch={branch_frags})"


def test_planning_a_BRANCH_reads_the_branch(ns) -> None:  # noqa: ANN001
    """THE DEFECT. A plan for the branch must see the branch's fragments.

    Asserted on the plan's own `read_version`, because that is what `commit_compaction` later commits
    against: a plan carrying main's version would have the worker rewrite against the wrong ref.
    """
    location = _location(ns)
    branch_version = open_dataset(ns, {}, TABLE_ID, branch=BRANCH).version

    plan = plan_compaction(location, {}, branch=BRANCH, target_rows_per_fragment=1024, batch_size=64, num_threads=2)

    assert plan.read_version == branch_version, f"the plan read version {plan.read_version} while the branch is at {branch_version} — it planned against main"
    assert plan.tasks, "the branch has five single-row fragments and the plan found nothing to merge"


def test_planning_without_a_branch_still_reads_main(ns) -> None:  # noqa: ANN001
    """Pinned so the fix cannot be "always open the branch"."""
    main_version = open_dataset(ns, {}, TABLE_ID).version

    plan = plan_compaction(_location(ns), {}, target_rows_per_fragment=1024, batch_size=64, num_threads=2)

    assert plan.read_version == main_version
