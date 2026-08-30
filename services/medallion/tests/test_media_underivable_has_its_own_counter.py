"""MED-016: undecodable media must not inflate the quality-gate counter.

`medallion.stage.quality_blocked` is defined as "BLOCKED by the quality gate (a data-quality assertion
failed)" — and the `UnderivableMediaError` path runs no assertion at all (`assert_quality` only runs on
the compute branch; the gate's own increment is the separate one under `if quality_blocked:`). Bumping
the gate's counter for a decode failure makes the "quality blocked /s" series lie twice: an operator
tuning gate thresholds chases blocks the gate never issued, and the real signal — deterministic bad
bytes arriving in bronze — has no series of its own. The DROP + FAIL-run contract is shared with the
gate on purpose; the metric is not.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from dapr.aio.clients import DaprClient

from medallion.core.config import MedallionSettings
from medallion.services import transform as tf
from medallion.services.derivers import UnderivableMediaError


class _FakeDapr:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


def _settings(tmp_path: Any) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "from_uri": str(tmp_path / "bronze"),
            "to_uri": str(tmp_path / "silver"),
            "from_namespace": "bronze",
            "from_dataset": "bronze$media",
            "to_namespace": "silver",
            "to_dataset": "silver$derived",
            "operation": "derive",
            "pub_topic": "medallion.silver",
        }
    )


def test_underivable_media_bumps_its_own_counter_and_not_the_quality_gates(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise UnderivableMediaError("payload matched the content probe but cannot decode")

    # Raised from the read — the branch under test keys on the exception TYPE, and the read is the
    # first statement inside the `try`, so no real upstream dataset is needed on disk.
    monkeypatch.setattr(tf, "read_upstream", _boom)

    quality_bumps: list[str] = []
    media_bumps: list[str] = []
    monkeypatch.setattr(tf, "record_quality_blocked", quality_bumps.append)
    # raising=False: before the fix the module has no `record_media_underivable` at all — the test
    # then fails on the assertions below (the gate counter took the bump), which is the finding.
    monkeypatch.setattr(tf, "record_media_underivable", media_bumps.append, raising=False)

    dapr, settings = _FakeDapr(), _settings(tmp_path)
    status = asyncio.run(tf.handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok-media", "originator": "alice"}}))

    assert status == {"status": "DROP"}, f"the DROP contract must survive the metric split: {status}"
    assert quality_bumps == [], "an undecodable payload ran no quality assertion — it must not count as a quality-gate block"
    assert media_bumps == ["bronze->silver"], f"the media failure left no series of its own: {media_bumps}"
