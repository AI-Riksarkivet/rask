"""#4 — durable object-store outbox for lineage events (the crash-window durability primitive)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, cast

from service_kit.lakehouse import outbox


def _uri(tmp_path: Any) -> str:
    return f"file://{tmp_path}/_lineage_outbox"


def test_drop_absent_is_idempotent(tmp_path: Any) -> None:
    outbox.drop_event(_uri(tmp_path), {}, "ghost")  # no raise on a missing object


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


def test_a_failed_stage_still_attempts_the_publish(tmp_path: Any, monkeypatch: Any) -> None:
    # Staging is a DURABILITY aid, not a precondition for delivery. Raising past the publish turns a
    # transient object-store blip into the one outcome the outbox exists to prevent: an event that
    # reaches nobody. An unstaged event that reaches the bus is delivered; an unstaged event that is
    # never published is gone, so publishing anyway strictly dominates.
    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("outbox bucket unreachable")

    monkeypatch.setattr(outbox, "stage_event", _boom)
    dapr = _Dapr()
    event = '{"run":{"runId":"r1"}}'
    asyncio.run(
        outbox.publish_lineage_with_outbox(
            dapr,
            outbox_uri=_uri(tmp_path),
            storage_options={},
            run_id="r1",
            event_json=event,
            pubsub_name="p",
            topic_name="t",
            timeout_seconds=5,
        )
    )
    assert dapr.published == [event]


# --------------------------------------------------------------------------- #
# the RELAY — the lineage service re-ingests staged survivors + drops poison
# --------------------------------------------------------------------------- #


class _Repo:
    def __init__(self) -> None:
        # ONE list, because there is one call: `ingest_event` writes the graph AND the durable /events
        # row in a single transaction. A recovered run that reached /runs + /producers while silently
        # absent from the /events audit surface is no longer expressible at this seam.
        self.ingested: list[str] = []
        self.refusals: list[dict[str, str | None]] = []

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        """[[LH-182]] The drain now RECORDS a settled refusal before retiring the object, so a double
        that cannot record one no longer stands in for the repository. Captured rather than ignored:
        several of these tests assert what the refusal path did, and a silent no-op would let a drain
        that recorded nothing pass as one that did."""
        self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})

    async def ingest_event(self, event: Any) -> None:
        self.ingested.append(event.run.run_id)


class _Settings:
    def __init__(self, uri: str, drain_limit: int = 500) -> None:
        self.outbox_uri = uri
        self.outbox_drain_limit = drain_limit  # the P1.2 per-tick cap (0 = unbounded)
        # What the relay re-publishes a recovered event to. Present on the double because the drain
        # reads them: a double missing a field the code under test uses fails as an AttributeError
        # swallowed by the tick's error boundary, which reads as a product bug rather than a gap here.
        self.dapr_pubsub = "lineage-pubsub"
        self.dapr_topic = "lineage.events.v1"
        self.dapr_publish_timeout_seconds = 5.0
        # The relay authorizes a staged event before ingesting it (`enforce_bus_authz`), which reads this
        # first and returns immediately when it is false. OFF here on purpose: these tests pin the drain's
        # INGEST/PUBLISH/DROP mechanics, and the gate's own behaviour is pinned by
        # `test_the_outbox_relay_refuses_what_the_bus_door_refuses.py`. Absent rather than false, it would
        # have failed as an AttributeError the tick's error boundary swallows into `stranded` — the exact
        # failure this class's comment above warns about, met on the first change that read a new field.
        self.fga_enabled = False


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


# --- one run, MANY events: the staged object must not collide ------------------------------------
#
# `build_run_event` deliberately excludes event_type from the run id — one run has a START, a COMPLETE
# or a FAIL, and they share it. `stage_event` keyed on the run id ALONE, so the second event for a run
# truncated the first, and the one that survived was whichever raced last.
#
# `transform.py:508-516` documents that happening to the event the outbox exists to preserve: "a
# COMPLETE whose PUBLISH failed left `completed = False`, the handler below staged a FAIL, and that
# truncating write destroyed the staged COMPLETE — the exact object the outbox exists to preserve, on a
# run whose Lance write had already committed." The workaround was to set a flag BEFORE the emit.
#
# Widening the key is backward-compatible by construction: `list_events` derives the drop key from the
# FILENAME, so an object staged under the old shape still lists and still drops.


def _event(run_id: str, event_type: str) -> str:
    return json.dumps({"eventType": event_type, "run": {"runId": run_id}})


def test_a_complete_and_a_fail_for_one_run_are_both_staged(tmp_path: Any) -> None:
    uri = _uri(tmp_path)

    outbox.stage_event(uri, {}, "run-1", _event("run-1", "COMPLETE"))
    outbox.stage_event(uri, {}, "run-1", _event("run-1", "FAIL"))

    staged = {json.loads(payload)["eventType"] for _key, payload in outbox.list_events(uri, {})}
    assert staged == {"COMPLETE", "FAIL"}, f"the second event truncated the first — one run's events share an object: {staged}"


def test_each_staged_event_drops_independently(tmp_path: Any) -> None:
    """Dropping the FAIL after its publish must not take the still-unpublished COMPLETE with it."""
    uri = _uri(tmp_path)
    outbox.stage_event(uri, {}, "run-1", _event("run-1", "COMPLETE"))
    outbox.stage_event(uri, {}, "run-1", _event("run-1", "FAIL"))

    for key, payload in list(outbox.list_events(uri, {})):
        if json.loads(payload)["eventType"] == "FAIL":
            outbox.drop_event(uri, {}, key)

    left = [json.loads(payload)["eventType"] for _key, payload in outbox.list_events(uri, {})]
    assert left == ["COMPLETE"], f"dropping one event disturbed the other: {left}"


def test_the_drain_RE_PUBLISHES_so_a_recovered_event_can_restart_a_halted_cascade(tmp_path: Any, monkeypatch: Any) -> None:
    """Ingesting alone repairs the GRAPH and leaves every subscriber unaware.

    The catalog's write announcement is what medallion's `/bronze-arrival` subscription reacts to, so a
    lost head event does not merely under-report provenance -- the whole bronze->silver->gold run never
    happens. The relay recovering it into the graph fixes the record and leaves the run halted forever,
    which is provenance restored and work still undone. Re-publishing is what makes the outbox a recovery
    mechanism rather than an audit repair.

    Ordering is asserted, not assumed: the publish must happen BEFORE the staged object is dropped, so a
    publish that fails leaves the event for the next tick. The re-ingest then is a no-op (MERGE on run_id).
    """
    from lineage.api import reconcile_cron
    from lineage.services import staged
    from medallion.schemas.events import build_run_event

    uri = _uri(tmp_path)
    event = build_run_event(
        operation="ingest_events",
        author="alice",
        job_namespace="medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=2,
        token="t1",
    )
    run_id = event["run"]["runId"]
    outbox.stage_event(uri, {}, run_id, json.dumps(event))

    published: list[dict[str, Any]] = []
    still_staged_at_publish: list[int] = []

    async def _fake_publish(_client: Any, **kwargs: Any) -> None:
        published.append(kwargs)
        # The staged object must still exist HERE. If the drop came first, a failed publish would have
        # destroyed the event's only durable copy -- the exact loss the outbox exists to prevent.
        still_staged_at_publish.append(len(list(outbox.list_events(uri, {}))))

    monkeypatch.setattr(staged.dapr_publish, "publish_event", _fake_publish)

    outcome = asyncio.run(reconcile_cron._drain_outbox(_authorized_request(), cast("Any", _Repo()), cast("Any", _Settings(uri)), {}, object()))

    assert outcome.drained == 1
    assert len(published) == 1, "a recovered event was ingested but never re-published -- the cascade stays halted"
    assert published[0]["topic_name"] == "lineage.events.v1"
    assert published[0]["pubsub_name"] == "lineage-pubsub"
    assert json.loads(published[0]["data"])["run"]["runId"] == run_id
    assert still_staged_at_publish == [1], "the staged object was dropped before the publish succeeded"
    assert list(outbox.list_events(uri, {})) == []  # ...and dropped once it did


def _authorized_request() -> Any:
    """A Request stand-in for the drain's authorization gate, with FGA OFF.

    `enforce_bus_authz` returns immediately when `settings.fga_enabled` is false, and these `_Settings`
    doubles do not enable it — so the gate is a no-op here and every assertion below still pins what it
    always pinned: the drain's INGEST/PUBLISH/DROP behaviour. The authorization behaviour itself is
    pinned by `test_the_outbox_relay_refuses_what_the_bus_door_refuses.py`, which is where it belongs.
    """
    return cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
