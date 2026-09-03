"""The on-demand compaction door hands the rewrite to the maintenance queue instead of doing it inline.

``POST /v1/table/{id}/maintenance/compact`` rewrote every fragment of the named table INSIDE the request
handler. The work is unbounded in the only dimension that matters here — a table's fragment count is a
property of the data, not of the request — so a click on a large table held a threadpool slot for as long
as the rewrite took, and the caller held an HTTP connection for the same span with no handle on the work
and no way to learn its outcome after a timeout. Both of those are the shape ``services/maintenance``
already fixed for the scheduled lane: the tick PLANS and publishes, a subscription executes one dataset.

So this door becomes a producer for that same lane. What crosses is a :class:`DatasetWorkItem`, the unit
the executor already consumes — not a second message type — which is why the model had to move to
``service_kit.lakehouse.work_items`` where both services can name it. The bounded half of the work stays
here: resolving the identifier, and the ``sibling_base_refs`` pre-pass whose verdict rides the unit.

**The inline lane survives, and is not a fallback bolted on.** ``register_work_route`` registers the
executor only when a work topic is configured, and ``routes.on_cron`` sweeps serially when it is not —
a deployment without a queue has no worker, so a 202 there would accept work nothing will ever perform.
This door reads the same setting and makes the same choice, which is why the two lanes cannot disagree
about whether a queue exists.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace, get_settings, get_storage_options
from catalog.api.v1.endpoints import maintenance as door
from catalog.core.config import Settings
from service_kit.lakehouse.ns_errors import install_problem_handlers


def _settings(*, topic: str) -> Settings:
    """The credentials are required fields and irrelevant here — the door never opens a real store."""
    return Settings(
        LANCE_S3_ACCESS_KEY_ID="k",
        LANCE_S3_SECRET_ACCESS_KEY="s",
        LANCE_MAINTENANCE_WORK_TOPIC=topic,
    )


class _Published:
    """Records what the door handed the sidecar, without a broker."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish_event(self, publisher: object, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> _Published:
    recorder = _Published()
    monkeypatch.setattr(door.dapr_publish, "publish_event", recorder.publish_event)
    return recorder


@pytest.fixture
def compacted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records an INLINE rewrite — the thing the queued lane must not do."""
    seen: list[str] = []

    def _compact_now(ds: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(str(getattr(ds, "uri", "?")))
        return {"ok": True, "fragments_removed": 3, "fragments_added": 1}

    monkeypatch.setattr(door.maintenance, "compact_now", _compact_now)
    return seen


def _app(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.state.dapr_client = object()

    class _Ds:
        uri = "s3://warehouse/aa3bed10_ns$events"

    monkeypatch.setattr(door, "open_dataset", lambda ns, so, segments: _Ds())
    monkeypatch.setattr(door.base_refs, "sibling_base_refs", lambda uri, so: door.base_refs.BaseRefs())
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: object()
    application.dependency_overrides[get_storage_options] = lambda: {}
    return application


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    settings = _settings(topic="maintenance.work.v1")
    with TestClient(_app(settings, monkeypatch, tmp_path)) as client:
        yield client


@pytest.fixture
def inline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    settings = _settings(topic="")
    with TestClient(_app(settings, monkeypatch, tmp_path)) as client:
        yield client


def test_with_a_queue_the_door_accepts_and_does_not_rewrite_in_the_handler(queued: TestClient, published: _Published, compacted: list[str]) -> None:
    response = queued.post("/v1/table/ns$events/maintenance/compact", json={"target_rows_per_fragment": 262144})
    assert response.status_code == 202, response.text
    assert compacted == [], "the request handler performed the rewrite it was supposed to enqueue"
    assert len(published.calls) == 1, f"expected exactly one unit on the work topic, got {published.calls}"


def test_the_enqueued_unit_is_the_one_the_executor_already_consumes(queued: TestClient, published: _Published, compacted: list[str]) -> None:
    """A second message type would need a second executor. The unit must round-trip as `DatasetWorkItem`."""
    from service_kit.lakehouse.work_items import DatasetWorkItem

    queued.post("/v1/table/ns$events/maintenance/compact", json={"target_rows_per_fragment": 262144})
    item = DatasetWorkItem.model_validate_json(published.calls[0]["data"])
    assert item.uri == "s3://warehouse/aa3bed10_ns$events"
    assert item.plan.target_rows_per_fragment == 262144
    assert item.plan.skipped is None, "an enqueued unit the planner would skip is work the executor drops"


def test_the_enqueued_unit_reclaims_nothing(queued: TestClient, published: _Published, compacted: list[str]) -> None:
    """The door is documented NON-DESTRUCTIVE ('writes a new version, removes none'), and the executor
    runs the full ordered pass. Both destructive steps must be off, or 'compact now' silently became
    'compact and reclaim history now' the moment the work moved lanes."""
    from service_kit.lakehouse.work_items import DatasetWorkItem

    queued.post("/v1/table/ns$events/maintenance/compact", json={})
    item = DatasetWorkItem.model_validate_json(published.calls[0]["data"])
    assert item.plan.cleanup_enabled is False
    assert item.plan.optimize_indices_enabled is False


def test_the_protection_verdict_rides_the_unit(monkeypatch: pytest.MonkeyPatch, queued: TestClient, published: _Published, compacted: list[str]) -> None:
    """`protected_by` is the whole reason a work item can leave this process. A door that enqueued
    without it would hand the executor a dataset whose shallow-clone source is invisible to it."""
    from service_kit.lakehouse import base_refs
    from service_kit.lakehouse.work_items import DatasetWorkItem

    monkeypatch.setattr(door.base_refs, "sibling_base_refs", lambda uri, so: base_refs.BaseRefs(protected={base_refs.normalise(uri)}))
    queued.post("/v1/table/ns$events/maintenance/compact", json={})
    item = DatasetWorkItem.model_validate_json(published.calls[0]["data"])
    assert item.protected_by == base_refs.normalise("s3://warehouse/aa3bed10_ns$events")


def test_without_a_queue_the_door_stays_synchronous(inline: TestClient, published: _Published, compacted: list[str]) -> None:
    """No work topic means `register_work_route` registered no executor. A 202 here accepts work that
    nothing will ever perform."""
    response = inline.post("/v1/table/ns$events/maintenance/compact", json={})
    assert response.status_code == 200, response.text
    assert response.json()["fragments_removed"] == 3
    assert compacted == ["s3://warehouse/aa3bed10_ns$events"]
    assert published.calls == []
