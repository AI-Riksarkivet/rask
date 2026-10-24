"""A refused dataset must reach the counter with the GATE that refused it.

Measured on the deployed estate 2026-09-24: **45.3-46.5% of every sweep is refused**, steady across
fourteen hours at ~600 datasets a tick. A standing condition that large has to be actionable, and the
unlabelled series could not say which of four things it was — somebody else's clone
(`protected_base`, true forever), a manifest feature a pylance upgrade would support
(`manifest_flags`), an unparseable branch directory (`invalid_ref`) or a missing grant
(`vend_denied`). Each has a different owner and a different fix.

THE BREAKDOWN ALREADY EXISTED AND STOPPED SHORT OF THE METRIC. `DatasetResult.refused_by` carries it
and `summarize_refusals` counts by it, into the sweep's one WARNING and the response body — the same
shape `compaction.bytes.reclaimed` had before it reached a series.

THE ZERO STAYS UNLABELLED, and that is not an oversight: the caller cannot know which gates exist on a
tick that refused nothing, and a `refused_by="none"` would put a value in the label set that names no
gate. `sum(rate(...))` is unaffected; `sum by (refused_by)` reads the breakdown.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from maintenance.core import metrics
from maintenance.core.config import MaintenanceSettings
from maintenance.services.expiry import DatasetResult


@pytest.fixture
def added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any] | None]]:
    seen: list[tuple[int, dict[str, Any] | None]] = []
    monkeypatch.setattr(metrics._refused, "add", lambda amount, attributes=None, **_: seen.append((amount, attributes)))
    return seen


def _ids(start: int, stop: int) -> pa.Table:
    return pa.table({"id": pa.array(range(start, stop), pa.int64())})


def _mixed_table(root: Path) -> str:
    """A 2.1 table with a 2.2 append: reader flag 256, which this pass cannot rewrite."""
    uri = str(root / "mixed.lance")
    lance.write_dataset(_ids(0, 4), uri, data_storage_version="2.1", enable_stable_row_ids=True)
    lance.write_dataset(_ids(4, 6), uri, mode="append", data_storage_version="2.2")
    return uri


def test_a_refusal_carries_the_gate(*, tmp_path: Path, maintain_one: Callable[..., DatasetResult], added: list[tuple[int, dict[str, Any] | None]]) -> None:
    uri = _mixed_table(tmp_path)

    result = maintain_one(uri, MaintenanceSettings.model_validate({"s3_bucket": "b"}))

    assert result.refused_by == "manifest_flags", f"the dataset was not refused by manifest_flags: {result}"
    assert added == [(1, {"refused_by": "manifest_flags"})], f"the gate did not reach the series: {added}"
