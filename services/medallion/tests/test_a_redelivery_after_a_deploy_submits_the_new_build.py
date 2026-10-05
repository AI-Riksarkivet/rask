"""A trigger redelivered after a deploy submits the NEW build's job (CP-029 clause a).

The run's one name is `derive_idempotency_key(stage, token, from, to, code_version)`: the plan's action id, the Ray
submission id and the outcome door's key. A redelivery under the same build finds its plan and re-attaches; under a new
build (`MEDALLION_RAY_CODE_VERSION` is the image the pod runs) it is a different run, so the new build's job is
submitted rather than the old build's outcome re-attached.

Driven through the stage runner's real pass 1 (`handle_stage`) on real pylance, into a plan store on `tmp_path`; the
Ray Jobs API that accepts the submission is an httpx transport recording each posted id.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from dapr.aio.clients import DaprClient

from medallion.core.config import MedallionSettings
from medallion.services import ray_submit, stage_plans
from medallion.services.compute import seed_bronze
from medallion.services.transform import handle_stage


class _Bus:
    """The Dapr client a stage runner holds; pass 1 publishes only its START through it."""

    async def publish_event(
        self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str = "application/json", publish_metadata: dict[str, str] | None = None
    ) -> None:
        return None


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ids: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/jobs/":
            ids.append(json.loads(request.content)["submission_id"])
            return httpx.Response(200, json={"submission_id": ids[-1]})
        return httpx.Response(404)

    client = httpx.AsyncClient(base_url="http://ray-head:8265", transport=httpx.MockTransport(handle))

    async def _client() -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(ray_submit, "ray_client", _client)
    return ids


def test_two_triggers_under_two_code_versions_submit_two_jobs(tmp_path: Path, posted: list[str]) -> None:
    bronze = tmp_path / "bronze"
    seed_bronze(str(bronze), {}, rows=4)
    base: dict[str, Any] = {
        "compute_enabled": True,
        "ray_enabled": True,
        "control_root": str(tmp_path / "control"),
        "from_uri": str(bronze),
        "to_uri": str(tmp_path / "silver"),
        "from_dataset": "bronze$events",
        "to_namespace": "silver",
        "to_dataset": "silver$features",
        "fga_service_identity": "service-bronze-to-silver",
    }
    builds = [MedallionSettings.model_validate(base | {"ray_code_version": tag}) for tag in ("lakehouse-aaaa", "lakehouse-bbbb")]

    for settings in builds:
        assert asyncio.run(handle_stage(cast(DaprClient, _Bus()), settings, {"data": {"token": "tok-1"}})) == {"status": "SUCCESS"}

    assert len(set(posted)) == 2, f"the second build re-used the first build's run: {posted}"
    planned = sorted(action_id for action_id, _ in stage_plans.plan_store(builds[0]).open_entries())
    assert planned == sorted(posted), "the plans and the submissions must name the same two runs"
