"""The producer as deployed forwards a stage read to the stage runner that hosts the run, as itself.

[[LH-220]] clause 5's stage legs (show and terminate) run through this forward, and so does the producer's hop to
the stage runners' door with its own projected `rask-medallion` token. Measured in review on c9543fac: the
producer's lifespan built no HTTP client, so every stage show and terminate answered 503 before any tenant check,
while the door's own tests passed on a client each of them injected.

Driven at `medallion.producer:app` through its own lifespan, with authentication off as the suite runs it. The stage
runner is the outbound collaborator under test, stood in for by respx at the URL the chart renders; the Dapr sidecar,
which this door never calls, is a stand-in so the lifespan does not wait for one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import respx
from fastapi.testclient import TestClient

from medallion.core.config import get_settings


RUNNER, INSTANCE = "bronze-to-silver", "stage-abc"
RUNNER_URL = "http://rask-bronze-to-silver.invalid:8000"


class _Sidecar:
    """What the lifespan does with its Dapr client: build it, and close it at shutdown."""

    async def close(self) -> None:
        return None


@pytest.fixture
def producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    token = tmp_path / "token"
    token.write_text("the-producers-projected-token\n")
    monkeypatch.setenv("MEDALLION_STAGE_RUNNER_URLS", json.dumps({RUNNER: RUNNER_URL}))
    monkeypatch.setenv("RASK_MEDALLION_IDENTITY_TOKEN_FILE", str(token))
    get_settings.cache_clear()
    from medallion import producer as deployed

    monkeypatch.setattr(deployed, "DaprClient", _Sidecar)
    with TestClient(deployed.app) as client:
        yield client
    get_settings.cache_clear()


def test_a_stage_read_reaches_the_runner_with_the_producers_own_token(producer: TestClient, respx_mock: respx.MockRouter) -> None:
    hosted = respx_mock.get(f"{RUNNER_URL}/stages/{INSTANCE}").respond(200, json={"instance_id": INSTANCE, "project": "", "status": "RUNNING"})

    answered = producer.get(f"/stage-runners/{RUNNER}/stages/{INSTANCE}")

    assert answered.status_code == 200, answered.text
    assert hosted.call_count == 1
    assert hosted.calls.last.request.headers["authorization"] == "Bearer the-producers-projected-token"
