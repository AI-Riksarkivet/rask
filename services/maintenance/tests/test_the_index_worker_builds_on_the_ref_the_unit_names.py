"""The index worker builds on the REF the unit names, not always on main.

[[LH-019]]. `maintenance/reindex` publishes and answers 202, so this worker is what actually opens
the dataset. Until `IndexWorkItem` carried a `branch`, the catalog door could only refuse one:
building main's index while the API reported the branch's is the wrong-but-plausible answer that row
exists to remove, and it is worse than a refusal because nothing downstream can tell.

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

from maintenance.services.index_build import build_index
from service_kit.lakehouse.work_items import SCALAR_INDEX, IndexWorkItem


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


def _unit(uri: str, column: str, **over: Any) -> IndexWorkItem:  # noqa: ANN401 — model kwargs are heterogeneous by construction
    base: dict[str, Any] = {"uri": uri, "table_id": "ns$t", "column": column, "kind": SCALAR_INDEX, "index_type": "BTREE", "name": f"{column}_idx"}
    return IndexWorkItem(**(base | over))


def test_the_worker_builds_on_the_branch_the_unit_names(diverged: str) -> None:
    outcome = build_index(_unit(diverged, "extra", branch="work"), write_options={})

    assert outcome.name == "extra_idx", outcome
    names = {index.name for index in lance.dataset(diverged).checkout_version(("work", None)).describe_indices()}
    assert "extra_idx" in names, f"the index did not land on the branch: {names}"


def test_a_branchless_unit_still_builds_on_main(diverged: str) -> None:
    """The control. Without it, a worker that read every unit as the branch would pass above."""
    outcome = build_index(_unit(diverged, "id"), write_options={})

    assert outcome.name == "id_idx", outcome
    names = {index.name for index in lance.dataset(diverged).describe_indices()}
    assert "id_idx" in names, f"the index did not land on main: {names}"


def test_a_branch_only_column_is_INVISIBLE_to_a_branchless_unit(diverged: str) -> None:
    """The fixture really did diverge the refs — otherwise every assertion here is about one dataset
    wearing two names, and a worker ignoring `branch` would pass the whole file."""
    with pytest.raises(Exception, match="extra"):
        build_index(_unit(diverged, "extra"), write_options={})
