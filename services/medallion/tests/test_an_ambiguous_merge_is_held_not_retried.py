"""[[LH-243]] A stage whose merge is ambiguous is held for quality, never retried.

A tier merges on `id`, and Lance refuses a merge whose source carries two rows matching one target row:
`Ambiguous merge inserts are prohibited` (measured on pylance 12.0.0). Redelivery cannot repair that — the
same upstream rows meet the same target rows every time — so a RETRY re-reads the upstream up to
maxDeliver times and then parks the trigger as if the stage runner had given up. The upstream holds a
repeated key, which is a data verdict: the run is recorded as failed, the hold is counted and visible in
the graph as BLOCKED, and the delivery is acked.

Reproduced the way the estate meets it, on real pylance: the upstream holds `id` 1 twice, the first run
lands both copies in the tier, and the second run's full-sync merge is the ambiguous one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import lance
from dapr.aio.clients import DaprClient

from medallion.core.config import MedallionSettings
from medallion.services.compute import seed_bronze
from medallion.services.transform import handle_stage


class _FakeDapr:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


def test_a_second_run_over_a_repeated_upstream_key_is_held_as_blocked_and_acked(tmp_path: Path) -> None:
    bronze, silver = str(tmp_path / "bronze"), str(tmp_path / "silver")
    seed_bronze(bronze, {}, rows=3)
    upstream = lance.dataset(bronze)
    upstream.insert(upstream.to_table(filter="id = 1"))
    settings = MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "from_uri": bronze,
            "to_uri": silver,
            "from_namespace": "bronze",
            "from_dataset": "bronze$events",
            "to_namespace": "silver",
            "to_dataset": "silver$features",
            "operation": "embed_features",
            "pub_topic": "silver.ready",
        }
    )
    assert asyncio.run(handle_stage(cast(DaprClient, _FakeDapr()), settings, {"data": {"token": "t1"}})) == {"status": "SUCCESS"}
    dapr = _FakeDapr()

    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t2"}}))

    assert status == {"status": "SUCCESS", "reason": "quality_blocked"}
    fails = [p["data"] for p in dapr.published if p["topic"] == settings.lineage_topic and p["data"]["eventType"] == "FAIL"]
    assert [event["run"]["facets"]["lance"].get("promotion_status") for event in fails] == [None, "BLOCKED"], fails
    assert "Ambiguous merge" in fails[0]["run"]["facets"]["errorMessage"]["message"]
