"""With a work topic configured, the cron tick PLANS and PUBLISHES — it maintains nothing itself.

That is the whole of what the queue buys, and each part is something the serial tick provably cannot do:

* an overrunning tick is QUEUED rather than dropped by the single-flight guard;
* a poison dataset fails its own message instead of stopping everything discovered after it;
* work outlives a pod restart, because JetStream holds it rather than a Python loop.

The ack decision is where a queue is easy to get wrong, and it is not one answer for every outcome.
`compact_one` never raises — it captures the per-dataset error — so a handler that always acked would
turn every transient S3 failure into a silently dropped dataset, and one that always retried would
recirculate an unreadable directory forever. The three outcomes are distinct and are pinned here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from maintenance.services.sweep import DatasetPlan, DatasetResult, DatasetWorkItem
from maintenance.services.work_queue import RETRY, SUCCESS, ack_for, enqueue_units


def _item(uri: str = "s3://bucket/t.lance") -> DatasetWorkItem:
    return DatasetWorkItem(uri=uri, plan=DatasetPlan(older_than=timedelta(days=7)))


class _Publisher:
    """A stand-in sidecar. Records every publish; can be told to fail on a chosen URI."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.published: list[dict[str, object]] = []
        self._fail_on = fail_on

    async def publish_event(self, **kwargs: object) -> None:
        data = kwargs.get("data")
        if self._fail_on and isinstance(data, str) and self._fail_on in data:
            raise RuntimeError("sidecar unavailable")
        self.published.append(kwargs)


@pytest.mark.asyncio
async def test_every_unit_becomes_its_own_message() -> None:
    """One message per dataset is what creates the per-dataset failure boundary.

    Batching them into one message would put the whole estate back behind a single ack and rebuild
    exactly the coupling the queue exists to break.
    """
    publisher = _Publisher()
    items = [_item("s3://b/a.lance"), _item("s3://b/b.lance"), _item("s3://b/c.lance")]

    published, failed = await enqueue_units(publisher, items, pubsub="ps", topic="maintenance.work.v1", timeout_seconds=5.0)

    assert (published, failed) == (3, [])
    assert len(publisher.published) == 3
    assert {call["topic_name"] for call in publisher.published} == {"maintenance.work.v1"}
    # The unit must cross as its own serialized document, not as a URI the worker has to re-plan from —
    # re-planning on the worker would lose the whole-estate protection verdict it cannot recompute.
    assert all("protected_by" in str(call["data"]) for call in publisher.published)


@pytest.mark.asyncio
async def test_a_publish_that_fails_is_COUNTED_not_swallowed() -> None:
    """A unit that never reached the queue is not maintained this tick.

    That is survivable — the next tick re-plans it — but only if it is visible. Silently dropping it
    makes a sidecar outage look identical to an estate with nothing to do.
    """
    publisher = _Publisher(fail_on="b.lance")
    items = [_item("s3://b/a.lance"), _item("s3://b/b.lance")]

    published, failed = await enqueue_units(publisher, items, pubsub="ps", topic="t", timeout_seconds=5.0)

    assert published == 1
    assert failed == ["s3://b/b.lance"]


@pytest.mark.parametrize(
    ("result", "expected", "why"),
    [
        (DatasetResult(uri="u", fragments_removed=3), SUCCESS, "work landed — nothing to redo"),
        (DatasetResult(uri="u"), SUCCESS, "a no-op tick on a healthy dataset is a success"),
        (DatasetResult(uri="u", refused="unsupported feature flag 16"), SUCCESS, "a refusal is a deliberate decline, not a failure"),
        (
            DatasetResult(uri="u", error="open: no such dataset", error_type="FileNotFoundError"),
            SUCCESS,
            "an unreadable directory is noise; redelivering it forever helps nobody",
        ),
        (DatasetResult(uri="u", error="maintain: connection reset", error_type="OSError"), RETRY, "a real failure must be redelivered, then dead-lettered"),
    ],
)
def test_the_ack_decision_distinguishes_the_three_outcomes(result: DatasetResult, expected: str, why: str) -> None:
    assert ack_for(result) == expected, why


@pytest.mark.asyncio
async def test_a_malformed_unit_is_ACKED_rather_than_redelivered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure redelivery cannot fix.

    A message that does not parse will not parse on the tenth attempt either, so retrying it only
    delays the DLQ while occupying a worker. The next tick re-plans that dataset from a planner that
    produces valid units, so acking loses nothing.
    """
    from maintenance.api import work as work_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("a malformed unit must never reach execution")

    monkeypatch.setattr(work_mod, "execute_unit", explode)
    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})

    assert await work_mod.handle_unit({"data": {"not": "a unit"}}, settings, NoopEmitter()) == {"status": SUCCESS}


@pytest.mark.asyncio
async def test_the_handler_relays_the_units_own_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine maintenance failure must reach the broker as RETRY.

    `compact_one` never raises, so nothing about the handler's own control flow signals the failure —
    the verdict has to be read off the result and relayed, or every transient S3 error becomes a
    silently dropped dataset.
    """
    from maintenance.api import work as work_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(work_mod, "emit_sweep_lineage", lambda *a, **k: _noop())
    # The handler re-reads protection before acting; stub the listing so this test stays about the ACK
    # decision rather than about object storage.
    monkeypatch.setattr(work_mod.base_refs, "sibling_base_refs", lambda uri, opts: work_mod.base_refs.BaseRefs())

    for error, expected in (("maintain: connection reset", RETRY), ("open: no such dataset", SUCCESS), (None, SUCCESS)):
        monkeypatch.setattr(work_mod, "execute_unit", lambda *a, error=error, **k: DatasetResult(uri="u", error=error, error_type="OSError" if error else None))
        answer = await work_mod.handle_unit({"data": _item().model_dump(mode="json")}, settings, NoopEmitter())
        assert answer == {"status": expected}, f"error={error!r}"


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_the_cron_tick_MAINTAINS_NOTHING_when_a_work_topic_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lane choice, at the route. This is the property the whole step is for.

    A tick that plans and publishes returns in the time it takes to enqueue, so the handler's cost is
    the estate's dataset COUNT rather than its total size — which is what stops an overrunning tick from
    being dropped by the single-flight guard. If `run_sweep` still ran on this lane, everything else
    here would be decoration on top of the same unbounded request.
    """
    import types
    from typing import Any, cast

    from maintenance.api import routes

    published: list[str] = []

    def never(*args: object, **kwargs: object) -> object:
        raise AssertionError("the queue lane must not maintain anything in the request")

    async def fake_enqueue(publisher: object, items: list[Any], **kwargs: object) -> tuple[int, list[str]]:
        published.extend(item.uri for item in items)
        return len(items), []

    async def noop_emit(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(routes, "run_sweep", never)
    monkeypatch.setattr(routes, "plan_sweep", lambda settings: ([_item("s3://b/a.lance"), _item("s3://b/b.lance")], []))
    monkeypatch.setattr(routes, "enqueue_units", fake_enqueue)
    monkeypatch.setattr(routes, "emit_sweep_lineage", noop_emit)
    settings = cast(Any, types.SimpleNamespace(delimiter="$", work_topic="maintenance.work.v1", work_pubsub="ps", publish_timeout_seconds=5.0))

    summary = await routes.on_cron(settings, cast(Any, object()), cast(Any, object()))

    assert summary == {"status": "enqueued", "planned": 2, "published": 2, "not_queued": 0, "skipped": 0}
    assert published == ["s3://b/a.lance", "s3://b/b.lance"]


@pytest.mark.asyncio
async def test_a_tick_with_no_sidecar_client_falls_back_to_the_serial_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """A work topic with no client to publish through must not silently maintain NOTHING.

    Failing over to the serial sweep is the safe direction: the estate still gets maintained. Choosing
    the queue lane anyway would enqueue zero units and report a healthy tick.
    """
    import types
    from typing import Any, cast

    from maintenance.api import routes

    ran: list[int] = []

    async def noop_emit(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(routes, "run_sweep", lambda settings: ran.append(1) or [])
    monkeypatch.setattr(routes, "emit_sweep_lineage", noop_emit)
    monkeypatch.setattr(routes, "summarize", lambda results: {"status": "ok"})
    settings = cast(Any, types.SimpleNamespace(delimiter="$", work_topic="maintenance.work.v1", work_pubsub="ps", publish_timeout_seconds=5.0))

    assert await routes.on_cron(settings, cast(Any, object()), None) == {"status": "ok"}
    assert ran == [1], "the serial lane never ran, so the estate went unmaintained"


@pytest.mark.asyncio
async def test_a_QUEUED_unit_reverifies_protection_before_acting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A unit's protection verdict was computed when it was PLANNED, and it can sit in the queue.

    The work stream is `workqueue` retention, which JetStream requires deliver-all consumers on — an
    unacked unit is replayed, by design, and can be up to the stream's max-age old. In that window a
    shallow clone may have been created whose source is this dataset, and compacting it would destroy
    the bytes the clone resolves through.

    Skipping the backlog is NOT the fix (it silently drops real work, and the broker refuses it here
    anyway). Re-reading the verdict is: `sibling_base_refs` is one non-recursive listing, computed per
    call for exactly this reason — "a clone created a minute ago must protect its source on the next
    click".
    """
    from maintenance.api import work as work_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter
    from service_kit.lakehouse import base_refs

    executed: list[str | None] = []

    def capture(item: object, **kwargs: object) -> DatasetResult:
        executed.append(getattr(item, "protected_by", None))
        return DatasetResult(uri="u")

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    # Planned clean; a clone appeared since, so the FRESH read says protected.
    monkeypatch.setattr(work_mod, "execute_unit", capture)
    monkeypatch.setattr(work_mod, "emit_sweep_lineage", noop)
    monkeypatch.setattr(base_refs, "sibling_base_refs", lambda uri, opts: base_refs.BaseRefs(protected={base_refs.normalise("s3://b/t.lance")}))

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    item = DatasetWorkItem(uri="s3://b/t.lance", plan=DatasetPlan(), protected_by=None)
    await work_mod.handle_unit({"data": item.model_dump(mode="json")}, settings, NoopEmitter())

    assert executed == [base_refs.normalise("s3://b/t.lance")], "the stale clean verdict was used instead of the fresh one"
