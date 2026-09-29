"""The on-demand compaction door hands the rewrite to the maintenance queue instead of doing it inline.

``POST /management/v1/table/{id}/maintenance/compact`` rewrote every fragment of the named table INSIDE the request
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
from service_kit.lakehouse import base_refs as sk_base_refs
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

    monkeypatch.setattr(door, "open_dataset", lambda ns, so, segments, **kwargs: _Ds())
    monkeypatch.setattr(sk_base_refs, "sibling_base_refs", lambda uri, so, *, configured, record_of: sk_base_refs.BaseRefs())
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
    response = queued.post("/management/v1/table/ns$events/maintenance/compact", json={"target_rows_per_fragment": 262144})
    assert response.status_code == 202, response.text
    assert compacted == [], "the request handler performed the rewrite it was supposed to enqueue"
    assert len(published.calls) == 1, f"expected exactly one unit on the work topic, got {published.calls}"


def test_the_protection_verdict_rides_the_unit(monkeypatch: pytest.MonkeyPatch, queued: TestClient, published: _Published, compacted: list[str]) -> None:
    """`protected_by` is the whole reason a work item can leave this process. A door that enqueued
    without it would hand the executor a dataset whose shallow-clone source is invisible to it."""
    from service_kit.lakehouse import base_refs
    from service_kit.lakehouse.work_items import DatasetWorkItem

    monkeypatch.setattr(
        sk_base_refs, "sibling_base_refs", lambda uri, so, *, configured, record_of: sk_base_refs.BaseRefs(protected={sk_base_refs.normalise(uri)})
    )
    queued.post("/management/v1/table/ns$events/maintenance/compact", json={})
    item = DatasetWorkItem.model_validate_json(published.calls[0]["data"])
    assert item.protected_by == base_refs.normalise("s3://warehouse/aa3bed10_ns$events")


def test_the_identity_survives_a_uri_no_parser_can_read(monkeypatch: pytest.MonkeyPatch, queued: TestClient, published: _Published) -> None:
    """`s3://lance-catalog/medallion/bronze` yields no id to `table_id_from_uri`. The door still knows.

    The executor asks the catalog for a credential scoped to the dataset it rewrites, and the catalog
    is addressed by IDENTIFIER (`POST /management/v1/table/{id}/credentials`), never by location. Measured
    over eleven top-level roots of the live warehouse, `table_id_from_uri` recovers an id from six: it
    reads the flat `<uuid8>_<table_id>` layout and returns None for the rest, including `medallion/`, the
    highest-churn writer in the estate. This door has the id in its own request path, so it stamps it.
    """
    from maintenance.core.lineage_emit import table_id_from_uri
    from service_kit.lakehouse.work_items import DatasetWorkItem

    class _MedallionDs:
        # The MEDALLION layout on purpose: this is the URI shape `table_id_from_uri` cannot read, so a
        # test using the flat layout would pass with the identity still coming from the path.
        uri = "s3://lance-catalog/medallion/bronze"

    monkeypatch.setattr(door, "open_dataset", lambda ns, so, segments, **kwargs: _MedallionDs())

    queued.post("/management/v1/table/bronze%24events/maintenance/compact", json={})
    item = DatasetWorkItem.model_validate_json(published.calls[0]["data"])
    assert table_id_from_uri(item.uri) is None, "pick a URI the parser genuinely cannot read, or this proves nothing"
    assert item.table_id == "bronze$events"


def test_without_a_queue_the_door_stays_synchronous(inline: TestClient, published: _Published, compacted: list[str]) -> None:
    """No work topic means `register_work_route` registered no executor. A 202 here accepts work that
    nothing will ever perform."""
    response = inline.post("/management/v1/table/ns$events/maintenance/compact", json={})
    assert response.status_code == 200, response.text
    assert response.json()["fragments_removed"] == 3
    assert compacted == ["s3://warehouse/aa3bed10_ns$events"]
    assert published.calls == []
