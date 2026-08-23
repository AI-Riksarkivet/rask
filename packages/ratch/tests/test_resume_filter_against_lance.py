"""The resume predicate, executed by Lance rather than asserted as a string.

`resume_filter` builds SQL that Lance's own scanner must parse. `IS DISTINCT FROM` is the load-bearing
choice — it is what makes a NULL `transform_version` (every row written before B4) count as stale — and
whether Lance's filter dialect accepts it is a fact about Lance, not about this repo. So this runs it.
"""

from __future__ import annotations

import pyarrow as pa
import pytest
from ratch.core.driver import TRANSFORM_VERSION_COLUMN, resume_filter


lance = pytest.importorskip("lance")


@pytest.fixture
def dataset(tmp_path):
    """Four rows covering every case the predicate must separate."""
    table = pa.table(
        {
            "id": [1, 2, 3, 4],
            "vector": ["v", "v", None, "v"],
            TRANSFORM_VERSION_COLUMN: ["cur", "old", "cur", None],
        }
    )
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(table, uri)
    return lance.dataset(uri)


def test_lance_accepts_the_widened_predicate(dataset) -> None:
    """If Lance cannot parse IS DISTINCT FROM, B4's predicate is broken and this is where it shows."""
    predicate = resume_filter("vector", identity="cur", has_version_column=True)
    assert dataset.count_rows(filter=predicate) >= 0, "predicate must parse"


def test_it_claims_exactly_the_stale_and_the_uncomputed(dataset) -> None:
    predicate = resume_filter("vector", identity="cur", has_version_column=True)
    claimed = sorted(dataset.to_table(filter=predicate, columns=["id"]).column("id").to_pylist())
    # 2 = computed by an OLD transform, 3 = never computed, 4 = written before the column existed.
    # 1 = computed by THIS transform, and must be left alone.
    assert claimed == [2, 3, 4]


def test_a_null_version_is_stale_not_skipped(dataset) -> None:
    """The `!=` form would drop row 4 silently; that is the bug IS DISTINCT FROM exists to avoid."""
    predicate = resume_filter("vector", identity="cur", has_version_column=True)
    claimed = dataset.to_table(filter=predicate, columns=["id"]).column("id").to_pylist()
    assert 4 in claimed


def test_the_narrow_predicate_still_claims_only_uncomputed_rows(dataset) -> None:
    """The pre-B4 behaviour, unchanged for datasets with no version column."""
    claimed = dataset.to_table(filter=resume_filter("vector"), columns=["id"]).column("id").to_pylist()
    assert claimed == [3]
