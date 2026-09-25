"""The re-run verb's cascade CLAIMS ride its trigger to the catalog's publish door, which caps them.

A re-run body's `cascade_id` and `originator` are copied onto the stage trigger (`build_stage_trigger`)
and the stage runner echoes both into `POST /management/v1/table/{id}/publish`, whose `PublishRequest`
bounds their length. A claim the verb accepts and that door refuses is a 202 for a hop that writes and
then RETRYs a deterministic 422 instead of promoting. So each row here is answered by the verb AND by
the real `PublishRequest`, joined over one bus.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx
from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import medallion.services.transform as stage_runner
from catalog.schemas import PublishRequest
from medallion.api import rerun as rerun_api
from medallion.api.dependencies import get_dapr, get_fga_client, get_settings
from medallion.core.config import MedallionSettings
from medallion.services import catalog_register, inprocess_executor
from medallion.services.compute import UpstreamFacts, WriteResult
from medallion.services.transform import handle_stage
from service_kit.lakehouse import warehouse_registry


_CATALOG = "http://catalog.test"
_PUBLISH = f"{_CATALOG}/management/v1/table/acme-gold$features/publish"


class _Bus:
    """Every publish the verb and the stage make, in order — one bus for both."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_: Any) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


@pytest.fixture(autouse=True)
def _fresh_registry_cache() -> Any:
    warehouse_registry.clear_cache()
    yield
    warehouse_registry.clear_cache()


@pytest.fixture
def stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MedallionSettings:
    """The governed silver->gold stage runner for tenant `acme`, its Lance IO and write-side catalog calls stubbed.

    Only the publish reaches the catalog over HTTP, because the publish body is the claim under test.
    """
    control = tmp_path / "control"
    (control / "_warehouses").mkdir(parents=True)
    (control / "_warehouses" / "wh1.json").write_text(json.dumps({"id": "wh1", "project": "acme", "root_uri": str(tmp_path / "acme-wh"), "status": "active"}))
    monkeypatch.setattr(stage_runner, "read_upstream", lambda uri, _options: UpstreamFacts(uri=uri, version=1))
    monkeypatch.setattr(inprocess_executor, "transform_stage", lambda *_a, **_k: WriteResult(version=2, row_count=1, size_bytes=1))
    monkeypatch.setattr(catalog_register, "ensure_stage_output", lambda **_: str(tmp_path / "acme-wh" / "gold.lance"))
    monkeypatch.setattr(catalog_register, "authorize_stage_write", lambda **_: "server_mediated")
    monkeypatch.setattr(catalog_register, "describe_table_location", lambda **_: None)
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "catalog_url": _CATALOG,
            "from_namespace": "silver",
            "from_dataset": "silver$features",
            "to_namespace": "gold",
            "to_dataset": "gold$features",
            "operation": "aggregate_gold",
            "pub_topic": "medallion.gold",
            "control_root": str(control),
        }
    )


def _catalog_publish(request: httpx.Request) -> httpx.Response:
    """The catalog's publish door, answering its body with its own request model."""
    try:
        PublishRequest.model_validate_json(request.content)
    except ValidationError as exc:
        return httpx.Response(422, json={"detail": exc.errors(include_url=False, include_context=False)})
    return httpx.Response(200, json={"published": True, "from_version": 1, "to_version": 2, "assertions": [], "accepted": []})


def _verb(bus: _Bus, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = MedallionSettings.model_validate(
        {"stage_runner_gates": {"silver": {"to_namespace": "gold", "required_action": "can_promote"}}, "transform_routes": {"silver": "medallion.silver"}}
    )

    async def _allowed(*_a: object, **_k: object) -> bool:
        return True

    monkeypatch.setattr(rerun_api.fga, "check", _allowed)
    app = FastAPI()
    app.include_router(rerun_api.router)
    app.state.dapr = bus
    app.dependency_overrides[get_dapr] = lambda: bus
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[rerun_api.authenticate_subject] = lambda: "alice"
    app.dependency_overrides[get_fga_client] = object
    return TestClient(app)


#: `(claim, length, whether the hop promotes)` — each claim at the publish door's cap and one past it.
_CLAIMS = [
    ("cascade_id", 128, True),
    ("cascade_id", 129, False),
    ("originator", 256, True),
    ("originator", 257, False),
]


@pytest.mark.parametrize(("claim", "length", "promotes"), _CLAIMS)
def test_the_rerun_verb_202s_exactly_the_claims_its_publish_accepts(
    stage: MedallionSettings, monkeypatch: pytest.MonkeyPatch, claim: str, length: int, promotes: bool
) -> None:
    """The re-run verb -> the silver->gold stage -> the catalog's publish door, on ONE bus.

    A 202'd claim must reach the publish and be accepted there; a claim that door would refuse is a
    422 at the verb, and nothing is published.
    """
    bus = _Bus()
    value = "c" * length
    body = {"object_id": "table:acme-silver$features", "project": "acme", "to_version": 2, "token": "t-1", claim: value}

    door = _verb(bus, monkeypatch).post("/stage-runners/stages/rerun", json=body)

    assert door.status_code == (202 if promotes else 422), door.text
    if not promotes:
        assert bus.published == [], "a refused claim published a stage trigger"
        return
    (trigger,) = bus.published
    with respx.mock(assert_all_called=True) as catalog:
        route = catalog.post(_PUBLISH).mock(side_effect=_catalog_publish)
        assert asyncio.run(handle_stage(cast(DaprClient, bus), stage, {"data": trigger["data"]})) == {"status": "SUCCESS"}
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content)[claim] == value
