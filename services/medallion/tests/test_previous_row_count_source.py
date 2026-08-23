"""The band must compare against the row count BEFORE the write, not against `version - 1`.

`previous_row_count(uri, version=result.version)` reads `version - 1` and calls that the predecessor.
For the cascade's own writes that is the wrong version, and `transform_stage`'s docstring says why
without noticing the consequence: the data lands in one commit, then "the index is a SECOND commit
(indices do not survive an `overwrite`), which is why the version this returns ... is read AFTER
both". So the reported version is N+1, `version - 1` is N — the commit that already holds the new
rows — and the delta is STRUCTURALLY ZERO.

Measured on the live estate 2026-08-23: 8 -> 200 rows published without a hold, then 200 -> 1000
published without a hold. A 5x jump against a +/-25% band cannot be unremarkable; the band was
comparing the write against itself.

The fix is to stop inferring the predecessor from version arithmetic. The writer knows the
destination's row count before it overwrites, so it records it — one observation at the only moment
it is unambiguous, instead of a reconstruction that depends on how many commits a write happens to
make. A destination that does not exist yet records `None`, which the band already reads as
FIRST_PROMOTION.
"""

from __future__ import annotations

import pyarrow as pa
import pytest
from medallion.services.compute import WriteResult, transform_stage


lance = pytest.importorskip("lance")


def test_write_result_carries_the_pre_write_row_count() -> None:
    assert "previous_row_count" in WriteResult.model_fields


def test_a_fresh_destination_reports_no_predecessor(tmp_path) -> None:
    """Nothing to compare against — the band reads this as a first promotion and asks."""
    src = str(tmp_path / "from.lance")
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), src)
    result = transform_stage(src, str(tmp_path / "to.lance"), {}, stage="silver")
    assert result.previous_row_count is None


def test_an_existing_destination_reports_what_it_held_BEFORE_the_write(tmp_path) -> None:
    """The regression. With `version - 1` this returned the new count and the delta was always zero."""
    src = str(tmp_path / "from.lance")
    dst = str(tmp_path / "to.lance")
    lance.write_dataset(pa.table({"id": list(range(8))}), src)
    transform_stage(src, dst, {}, stage="silver")

    lance.write_dataset(pa.table({"id": list(range(1000))}), src, mode="overwrite")
    result = transform_stage(src, dst, {}, stage="silver")

    assert result.row_count == 1000
    assert result.previous_row_count == 8, "the band would see a delta of zero and never ask"
