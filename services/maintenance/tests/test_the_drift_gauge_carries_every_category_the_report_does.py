"""The drift METRIC and the drift REPORT must carry the same categories.

The gauge exists because the report gates the trash purge and answers whether the estate's storage
state is understood — "a number only a reader of pod logs can see is one no alert can fire on", in the
recorder's own words. It was emitted from inside `build_report`, which compares three STORES, while
`_orphan_category` runs afterwards and attaches the three categories that read STORAGE:
`unregistered_datasets`, `absent_datasets` and `orphan_files`.

MEASURED LIVE 2026-09-24. The pod logged thirteen categories and GreptimeDB held **ten** — confirmed by
querying the three by name (`NO SERIES`) rather than by reading a truncated list, and over a two-hour
range rather than an instant. Two of the three missing ones were NON-ZERO — `absent_datasets=9`,
`orphan_files=1` — and both were BLOCKING the purge at that moment: `trash_purge_blocked ... 2
finding(s) across ['orphan_buckets', 'orphan_files']`. So the estate was refusing to reclaim bytes for
a reason no dashboard or alert could show.

THE FIX IS POSITION, NOT CONTENT, and this test pins the position by its consequence rather than by
reading the source: drive the real `reconcile()` and require the recorded keys to equal the reported
keys. A future pass that adds a fourteenth category after the recorder reds this the same day.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from maintenance.core import metrics
from maintenance.core.config import MaintenanceSettings
from maintenance.services import reconcile as mod


class _NoBuckets:
    """Enough S3 for `orphan_buckets` to be CHECKED, so the store half is provably present."""

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": []}


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, int]]:
    seen: list[dict[str, int]] = []
    monkeypatch.setattr(metrics, "record_drift", lambda counts: seen.append(dict(counts)))
    monkeypatch.setattr(mod.metrics, "record_drift", lambda counts: seen.append(dict(counts)))
    return seen


def _run(tmp_path: Path, *, orphan_scan: bool) -> mod.ReconcileReport:
    settings = MaintenanceSettings(
        s3_bucket="lance-catalog",
        s3_access_key_id="unit",
        s3_secret_access_key=SecretStr("unit"),
        orphan_scan_enabled=orphan_scan,
    )
    (tmp_path / "control").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return asyncio.run(
        mod.reconcile(
            settings,
            None,
            warehouses_enabled=False,
            control_root=f"file://{tmp_path / 'control'}",
            namespace_root=f"file://{tmp_path / 'data'}",
            bucket_client=_NoBuckets(),
        )
    )


def test_the_gauge_receives_exactly_what_the_report_carries(tmp_path: Path, recorded: list[dict[str, int]]) -> None:
    report = _run(tmp_path, orphan_scan=True)

    assert len(recorded) == 1, f"the drift gauge was written {len(recorded)} times, expected once per tick"
    assert recorded[0].keys() == report.counts.keys(), (
        "the metric and the report disagree about which categories were checked: "
        f"only-report={sorted(report.counts.keys() - recorded[0].keys())}, only-metric={sorted(recorded[0].keys() - report.counts.keys())}"
    )
    assert recorded[0] == report.counts, "the metric carries different VALUES than the report"


def test_the_storage_categories_are_among_them(tmp_path: Path, recorded: list[dict[str, int]]) -> None:
    """Anti-vacuity, and it names the three the defect was made of.

    Without this the test above passes on a report that carries only the store categories — exactly the
    state the bug produced — because both sides would then agree on ten.
    """
    _run(tmp_path, orphan_scan=True)

    assert recorded, "nothing was recorded at all"
    assert {"unregistered_datasets", "absent_datasets"} <= recorded[0].keys(), f"the storage-reading categories never reached the gauge: {sorted(recorded[0])}"


def test_a_SKIPPED_orphan_scan_still_records_what_was_checked(tmp_path: Path, recorded: list[dict[str, int]]) -> None:
    """The recorder's own discipline: a category that was not checked is OMITTED, never zeroed. Moving
    the call must not turn a skipped scan into a false zero on the series an alert fires from."""
    report = _run(tmp_path, orphan_scan=False)

    assert recorded, "nothing was recorded when the orphan scan was off"
    assert "orphan_files" not in recorded[0], "a skipped scan was recorded as zero, which reads as clean"
    assert recorded[0].keys() == report.counts.keys(), "the metric and the report disagree when the scan is off"
