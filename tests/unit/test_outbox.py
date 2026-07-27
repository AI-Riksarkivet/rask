"""#4 — durable object-store outbox for lineage events (the crash-window durability primitive)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, cast

import pytest

from service_kit.lakehouse import outbox


def _uri(tmp_path: Any) -> str:
    return f"file://{tmp_path}/_lineage_outbox"


def test_stage_list_drop_roundtrip(tmp_path: Any) -> None:
    uri = _uri(tmp_path)
    outbox.stage_event(uri, {}, "run-1", json.dumps({"run": {"runId": "run-1"}}))
    outbox.stage_event(uri, {}, "run-2", json.dumps({"run": {"runId": "run-2"}}))

    got = dict(outbox.list_events(uri, {}))
    assert set(got) == {"run-1", "run-2"}
    assert json.loads(got["run-1"])["run"]["runId"] == "run-1"

    outbox.drop_event(uri, {}, "run-1")
    assert set(dict(outbox.list_events(uri, {}))) == {"run-2"}


def test_drop_absent_is_idempotent(tmp_path: Any) -> None:
    outbox.drop_event(_uri(tmp_path), {}, "ghost")  # no raise on a missing object


def test_list_absent_prefix_is_empty(tmp_path: Any) -> None:
    assert list(outbox.list_events(f"file://{tmp_path}/never-created", {})) == []


class _Dapr:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[str] = []
        self.fail = fail

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        if self.fail:
            raise TimeoutError("sidecar down")
        self.published.append(data)


def test_publish_with_outbox_stages_then_drops_on_ack(tmp_path: Any) -> None:
    uri = _uri(tmp_path)
    dapr = _Dapr()
    event = '{"run":{"runId":"r1"}}'
    asyncio.run(
        outbox.publish_lineage_with_outbox(
            dapr,
            outbox_uri=uri,
            storage_options={},
            run_id="r1",
            event_json=event,
            pubsub_name="p",
            topic_name="t",
            timeout_seconds=5,
        )
    )
    assert dapr.published == [event]
    assert list(outbox.list_events(uri, {})) == []  # dropped after the publish acked


def test_publish_failure_leaves_event_staged_for_the_relay(tmp_path: Any) -> None:
    # THE crash-window guarantee: a failed/crashed publish leaves the full event in the outbox so the relay
    # can recover it — the exact loss window the pre-#4 fire-and-forget publish could not close.
    uri = _uri(tmp_path)
    dapr = _Dapr(fail=True)
    event = '{"run":{"runId":"r1"}}'
    with pytest.raises(TimeoutError):
        asyncio.run(
            outbox.publish_lineage_with_outbox(
                dapr,
                outbox_uri=uri,
                storage_options={},
                run_id="r1",
                event_json=event,
                pubsub_name="p",
                topic_name="t",
                timeout_seconds=5,
            )
        )
    assert dict(outbox.list_events(uri, {})) == {"r1": event}  # survived


def test_no_outbox_uri_degrades_to_plain_publish(tmp_path: Any) -> None:
    dapr = _Dapr()
    asyncio.run(
        outbox.publish_lineage_with_outbox(
            dapr,
            outbox_uri="",
            storage_options={},
            run_id="r1",
            event_json='{"x":1}',
            pubsub_name="p",
            topic_name="t",
            timeout_seconds=5,
        )
    )
    assert dapr.published == ['{"x":1}']  # published, nothing staged


# --------------------------------------------------------------------------- #
# the RELAY — the lineage service re-ingests staged survivors + drops poison
# --------------------------------------------------------------------------- #


class _Repo:
    def __init__(self) -> None:
        self.ingested: list[str] = []
        self.recorded: list[str] = []  # the durable /events feed rows the drain must ALSO write (finding 1)

    async def ingest_event(self, event: Any) -> None:
        self.ingested.append(event.run.run_id)

    async def record_event(self, *, run_id: str, **_fields: Any) -> None:
        # The drain must project onto the feed like the JetStream + HTTP ingest paths, else a recovered run
        # reaches /runs + /producers but is silently absent from the /events audit surface.
        self.recorded.append(run_id)


class _Settings:
    def __init__(self, uri: str, drain_limit: int = 500) -> None:
        self.outbox_uri = uri
        self.outbox_drain_limit = drain_limit  # the P1.2 per-tick cap (0 = unbounded)


def test_relay_drain_reingests_valid_and_drops_poison(tmp_path: Any) -> None:
    # The full-event recovery half: a well-formed staged event is re-ingested into the graph and deleted; a
    # poison (unparseable) object is dropped so it can't wedge the drain — nothing lingers afterward.
    from lineage.api.reconcile_cron import _drain_outbox
    from medallion.schemas.events import build_run_event

    uri = _uri(tmp_path)
    event = build_run_event(
        operation="ingest_events",
        author="alice",
        job_namespace="medallion",
        inputs=[("raw", "raw_events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=2,
        token="t1",
    )
    run_id = event["run"]["runId"]
    outbox.stage_event(uri, {}, run_id, json.dumps(event))
    outbox.stage_event(uri, {}, "poison-run", "{ not valid json")

    repo = _Repo()
    drained = asyncio.run(_drain_outbox(cast("Any", repo), cast("Any", _Settings(uri)), {}))

    assert drained == 1  # the valid event ingested; the poison was dropped, not ingested
    assert repo.ingested == [run_id]
    assert repo.recorded == [run_id]  # ...AND projected onto the durable /events feed (finding 1 guard)
    assert list(outbox.list_events(uri, {})) == []  # both objects gone (ingested / dropped)


def test_relay_drain_is_idempotent_on_reingest(tmp_path: Any) -> None:
    # A publish that DID land but whose producer crashed before deleting: the relay re-ingests (a graph
    # MERGE no-op) and deletes. Proven here by draining the same staged event twice with no error.
    from lineage.api.reconcile_cron import _drain_outbox
    from medallion.schemas.events import build_run_event

    uri = _uri(tmp_path)
    event = build_run_event(
        operation="ingest_events",
        author="alice",
        job_namespace="medallion",
        inputs=[("raw", "raw_events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=1,
        token="t2",
    )
    outbox.stage_event(uri, {}, event["run"]["runId"], json.dumps(event))
    repo = _Repo()
    assert asyncio.run(_drain_outbox(cast("Any", repo), cast("Any", _Settings(uri)), {})) == 1
    # re-stage + drain again → still fine (the ingest is idempotent on run_id)
    outbox.stage_event(uri, {}, event["run"]["runId"], json.dumps(event))
    assert asyncio.run(_drain_outbox(cast("Any", repo), cast("Any", _Settings(uri)), {})) == 1
    assert repo.ingested.count(event["run"]["runId"]) == 2  # called twice; the GRAPH MERGE makes it a no-op


# --------------------------------------------------------------------------- #
# BOUNDED DRAIN + SATURATION SNAPSHOT (GOAL-prove-it P1.1/P1.2)
# --------------------------------------------------------------------------- #


def test_backlog_reports_depth_and_oldest_age(tmp_path: Any) -> None:
    # The alertable pair. An outbox that silently stops draining — the one failure that loses lineage
    # forever — used to look exactly like a healthy one, because NOTHING was measured.
    uri = _uri(tmp_path)
    assert outbox.backlog(uri, {}) == (0, 0.0)  # empty must report 0, not go stale
    for i in range(3):
        outbox.stage_event(uri, {}, f"run-{i}", "{}")
    depth, age = outbox.backlog(uri, {})
    assert depth == 3
    assert age >= 0.0  # a real, non-negative age for the OLDEST staged event


def test_list_events_is_bounded_and_oldest_first(tmp_path: Any) -> None:
    # The drain used to materialise the WHOLE prefix inside the single-flight lock, so a backlog — exactly
    # what the outbox exists to survive — could OOM/stall the tick. Cap it, oldest-first so nothing starves.
    uri = _uri(tmp_path)
    for i in range(5):
        outbox.stage_event(uri, {}, f"run-{i}", "{}")
        time.sleep(0.01)  # distinct mtimes so "oldest first" is actually assertable

    first_two = [rid for rid, _ in outbox.list_events(uri, {}, limit=2)]
    assert first_two == ["run-0", "run-1"]  # bounded AND oldest-first (not arbitrary order)

    assert len({rid for rid, _ in outbox.list_events(uri, {})}) == 5  # no limit => everything


def test_bounded_drain_makes_progress_across_ticks(tmp_path: Any) -> None:
    # A capped tick must not starve the backlog: draining the cap, dropping those, then draining again
    # must eventually clear it — and always take the OLDEST first, so `oldest_age` falls monotonically.
    uri = _uri(tmp_path)
    for i in range(5):
        outbox.stage_event(uri, {}, f"run-{i}", "{}")
        time.sleep(0.01)

    seen: list[str] = []
    for _ in range(3):  # 3 ticks x cap 2 > 5 staged
        batch = list(outbox.list_events(uri, {}, limit=2))
        for rid, _payload in batch:
            seen.append(rid)
            outbox.drop_event(uri, {}, rid)

    assert seen == ["run-0", "run-1", "run-2", "run-3", "run-4"]  # strictly oldest-first, fully drained
    assert outbox.backlog(uri, {}) == (0, 0.0)
