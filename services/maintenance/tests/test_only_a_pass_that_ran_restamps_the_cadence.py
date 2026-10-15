"""A policy cadence paces a dataset's pass, and the pace is set by passes that ran, never by skips.

A `policy_interval` skip carries the very policy record whose cadence produced it, so a stamp written on a skip would
push the next pass out by another full interval on every tick, freezing the dataset forever behind the mechanism that
exists to pace it. A pass that ran must still stamp, or the cadence never advances.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services.expiry import DatasetResult
from maintenance.services.plan import DatasetPlan
from service_kit.lakehouse import maintenance_policies


@pytest.mark.parametrize(
    ("stamped", "skipped", "stamps"), [pytest.param("now", "policy_interval", 0, id="inside-the-interval"), pytest.param(None, None, 1, id="never-stamped")]
)
def test_only_a_pass_that_ran_restamps_the_cadence(
    *, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maintain_one: Callable[..., DatasetResult], stamped: str | None, skipped: str | None, stamps: int
) -> None:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), uri)
    written: list[str] = []
    monkeypatch.setattr(maintenance_policies, "read_state", lambda *_a, **_k: datetime.now(UTC).isoformat() if stamped else None)
    monkeypatch.setattr(maintenance_policies, "write_state", lambda *a, **_k: written.append(str(a)))
    settings = MaintenanceSettings(s3_bucket="b", s3_secret_access_key=SecretStr("unit"))

    result = maintain_one(uri, settings, plan=DatasetPlan(policy={"id": "p1", "compact_interval_hours": 6}))

    assert result.skipped == skipped, result
    assert len(written) == stamps, f"the cadence stamp was written {len(written)} time(s) for a {skipped or 'completed'} pass"
