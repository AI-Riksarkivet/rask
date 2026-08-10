"""The InboxActor, driven for real against a fake state manager and a recording reminder API.

Everything below is provable without a Dapr sidecar, a placement service or a database. What these
tests CANNOT prove is the runtime guarantee they rest on — that Dapr routes one actor id to one pod
and serialises calls to it — which needs a live sidecar and is the round-trip proof, not a unit test.

What they DO pin is everything the runtime does not give for free: that the unread count is derived
rather than adjusted, that the compaction reminder is armed before the rows it bounds and disarmed
after they are gone, that a reminder the Scheduler lost is repaired from the read path, and that a
record belonging to somebody else reads as UNREADABLE rather than absent.
"""

import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast, override

import pytest
from dapr.actor import ActorId
from pydantic import ValidationError

from notifications.config import get_notifications_settings
from notifications.errors import InboxUnreadable
from notifications.feed import unread_count
from notifications.inbox_actor import COMPACTION_REMINDER, META_KEY, ROWS_KEY, InboxActor, InboxActorInterface
from notifications.models import InboxMeta, InboxPointer, InboxRows, NotificationReason, notification_id
from notifications.proxies import inbox_actor_id


SUBJECT = "alice@example.test"
OTHER = "bob@example.test"


class _FakeStateManager:
    """The actor's state partitions, STAGED then flushed — the shape `ActorStateManager` really has.

    `set_state_ttl` does not reach the store; `save_state` is what commits, and a turn reads back its
    own pending writes. Modelling that rather than writing straight through is what lets a save be
    made to fail: a double that persisted on `set_state_ttl` would report a failed transition as
    durable, and the reminder-ordering proofs below would then be asserting nothing.
    """

    def __init__(self, events: list[str]) -> None:
        self.store: dict[str, str] = {}
        self.staged: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.saves = 0
        #: Set to make the transaction refuse — "the state store is down mid-transition".
        self.save_error: Exception | None = None
        self._events = events

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        if key in self.staged:
            return (True, self.staged[key])
        return (key in self.store, self.store.get(key))

    async def set_state_ttl(self, key: str, value: str, ttl_in_seconds: int | None) -> None:
        self.staged[key] = value
        self.ttls[key] = ttl_in_seconds

    async def save_state(self) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.store.update(self.staged)
        self.staged.clear()
        self.saves += 1
        self._events.append("save")


class _Actor(InboxActor):
    """The real actor with its Dapr plumbing replaced — state and reminders recorded in memory."""

    def __init__(self, subject: str = SUBJECT) -> None:
        # `sm` is the TYPED handle the assertions read; `_state_manager` is the same object cast into
        # the slot the actor reads. Casting only at the injection point keeps the double's own surface
        # honest rather than fighting the base class's type through every assertion.
        self.events: list[str] = []
        self.sm = _FakeStateManager(self.events)
        self._state_manager = cast(Any, self.sm)
        self.id = ActorId(inbox_actor_id(subject))
        self.armed: list[tuple[str, float, float]] = []
        #: The Scheduler's own view: keyed BY NAME, because `register_reminder` overwrites rather than
        #: appends. `armed` records the calls; this records what is left registered — and only the
        #: second can answer "did the repair leave two reminders behind".
        self.reminders: dict[str, tuple[float, float]] = {}
        self.unregistered: list[str] = []
        #: Set to make the Scheduler refuse — its etcd is a store of its own and can be down alone.
        self.register_error: Exception | None = None
        self.unregister_error: Exception | None = None

    async def register_reminder(
        self,
        name: str,
        state: bytes,
        due_time: timedelta,
        period: timedelta | None = None,
        ttl: timedelta | None = None,
        failure_policy: Any = None,
    ) -> None:
        """Signature mirrors `Actor.register_reminder` exactly. A narrower override would type-error,
        and — worse — would stop compiling the day the actor starts passing `failure_policy`."""
        if self.register_error is not None:
            raise self.register_error
        self.armed.append((name, due_time.total_seconds(), (period or timedelta(0)).total_seconds()))
        self.reminders[name] = (due_time.total_seconds(), (period or timedelta(0)).total_seconds())
        self.events.append(f"arm:{name}")

    async def unregister_reminder(self, name: str) -> None:
        if self.unregister_error is not None:
            raise self.unregister_error
        self.unregistered.append(name)
        self.reminders.pop(name, None)
        self.events.append(f"disarm:{name}")


def _delivery(run: str, state: str = "FAIL", *, minutes_ago: int = 0) -> dict[str, Any]:
    return {
        "notification_id": notification_id(run, state),
        "reason": NotificationReason.AUTHOR.value,
        "object_id": "silver/pages",
        "source_run_id": run,
        "occurred_at": (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(),
    }


def _meta(actor: _Actor) -> InboxMeta:
    return InboxMeta.model_validate_json(actor.sm.store[META_KEY])


def _rows(actor: _Actor) -> InboxRows:
    return InboxRows.model_validate_json(actor.sm.store[ROWS_KEY])


def _recount(actor: _Actor) -> tuple[int, int]:
    """`(unread, rows)` counted from the STORED rows — the second opinion the meta is checked against.

    Deliberately not `meta.unread`: the whole claim under test is that the count in the small partition
    is a derivation of the large one, so a test that read it from the same place would prove nothing.
    """
    pointers = _rows(actor).pointers
    return (unread_count(pointers), len(pointers))


def _actor_with_id(actor_id: str) -> _Actor:
    """An actor addressed by a RAW id.

    The only route to the decode failure: `inbox_actor_id` cannot produce an id that is not an encoded
    subject, and the actor is instantiated by the Dapr runtime from whatever the sidecar routed on.
    """
    actor = _Actor()
    actor.id = ActorId(actor_id)
    return actor


async def _read(actor: _Actor, path: str) -> None:
    """Drive one of the two READ doors. Both must repair a lost reminder — a badge poll is by far the
    more frequent of the two, so a repair that only lived on the panel's path would rarely run."""
    if path == "unread":
        await actor.unread()
        return
    await actor.page({"limit": 10})


@pytest.mark.asyncio
async def test_an_unknown_subject_reads_as_empty_never_as_unreadable() -> None:
    actor = _Actor()
    assert await actor.unread() == {"unread": 0, "rows": 0}
    page = await actor.page({"limit": 10})
    assert (page["pointers"], page["has_more"], page["unread"]) == ([], False, 0)


@pytest.mark.asyncio
async def test_marking_an_inbox_nobody_wrote_mints_no_state() -> None:
    """A read-shaped call must not create rows for a subject nobody has notified."""
    actor = _Actor()
    assert await actor.mark_seen({"notification_ids": ["run-1@FAIL"]}) == {"updated": 0, "unread": 0, "rows": 0}
    assert await actor.dismiss({"notification_id": "run-1@FAIL"}) == {"dismissed": 0, "unread": 0, "rows": 0}
    assert actor.sm.store == {}


@pytest.mark.asyncio
async def test_delivery_lands_a_pointer_and_the_badge_reads_the_meta_partition_alone() -> None:
    actor = _Actor()
    assert await actor.deliver(_delivery("run-1")) == {"delivered": True, "unread": 1, "rows": 1}

    # Drop the ROWS partition: if `unread` still answers, it demonstrably never read it. That is the
    # whole point of the split — the badge is the most frequent read in the plane.
    del actor.sm.store[ROWS_KEY]
    assert await actor.unread() == {"unread": 1, "rows": 1}


@pytest.mark.asyncio
async def test_delivery_is_idempotent_on_the_notification_id() -> None:
    """JetStream is at-least-once and the reconciler re-offers the same run over a second ingress, so
    the natural key IS the dedupe — there is no exactly-once and no window surviving a pod restart."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    again = await actor.deliver(_delivery("run-1"))
    assert again == {"delivered": False, "unread": 1, "rows": 1}
    assert len(_rows(actor).pointers) == 1


@pytest.mark.asyncio
async def test_a_runs_started_and_failed_are_different_notifications() -> None:
    actor = _Actor()
    await actor.deliver(_delivery("run-1", "COMPLETE"))
    await actor.deliver(_delivery("run-1", "FAIL"))
    assert {p.notification_id for p in _rows(actor).pointers} == {"run-1@COMPLETE", "run-1@FAIL"}


@pytest.mark.asyncio
async def test_dismissing_one_state_leaves_the_other_deliverable() -> None:
    """The id scheme's whole purpose: dismissing "this run started" must not silence "it failed"."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1", "COMPLETE"))
    await actor.dismiss({"notification_id": "run-1@COMPLETE"})
    assert await actor.deliver(_delivery("run-1", "FAIL")) == {"delivered": True, "unread": 1, "rows": 2}


@pytest.mark.asyncio
async def test_the_count_is_derived_so_repeating_a_mark_cannot_drift_it() -> None:
    """No etag, no OCC, and no counter to adjust: the count is recomputed from the rows every time, so
    a replayed mark is a no-op rather than a decrement."""
    actor = _Actor()
    for run in ("run-1", "run-2", "run-3"):
        await actor.deliver(_delivery(run))

    first = await actor.mark_seen({"notification_ids": ["run-1@FAIL", "run-2@FAIL"]})
    replay = await actor.mark_seen({"notification_ids": ["run-1@FAIL", "run-2@FAIL"]})
    assert (first["updated"], first["unread"]) == (2, 1)
    assert (replay["updated"], replay["unread"]) == (0, 1)


@pytest.mark.asyncio
async def test_marking_an_unknown_id_changes_nothing() -> None:
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    assert await actor.mark_seen({"notification_ids": ["run-9@FAIL"]}) == {"updated": 0, "unread": 1, "rows": 1}


@pytest.mark.asyncio
async def test_an_empty_mark_is_a_no_op_not_an_error() -> None:
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    assert (await actor.mark_seen({"notification_ids": []}))["unread"] == 1


@pytest.mark.asyncio
async def test_the_compaction_reminder_is_armed_before_the_rows_it_bounds_are_persisted() -> None:
    """Arm before, persist after. The reverse order can leave persisted rows whose only bound never
    got registered — and the reminder lives in the Scheduler's etcd, not beside the rows, so nothing
    downstream would ever notice."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    assert actor.events == [f"arm:{COMPACTION_REMINDER}", "save"]


@pytest.mark.asyncio
async def test_the_compaction_reminder_is_disarmed_after_the_rows_are_gone() -> None:
    """Disarm late, for the mirror reason: a disarm followed by a save that fails would leave rows with
    no bound at all."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1", minutes_ago=48 * 60))
    actor.events.clear()

    await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(0), timedelta(seconds=3600))

    assert _rows(actor).pointers == []
    assert actor.events[-2:] == ["save", f"disarm:{COMPACTION_REMINDER}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["unread", "page"])
async def test_a_read_repairs_a_reminder_the_scheduler_lost(read: str) -> None:
    """The Scheduler's etcd and `lance-statestore`'s Postgres are two stores with no transaction
    between them, and `ActorStateTTL` is off — so the compaction reminder is the only bound on an inbox
    AND it can vanish independently of the rows it bounds. Any interaction has to repair it.

    Both read doors, because the badge poll is the frequent one: a repair that lived only on the panel's
    path would run on the door an unattended tab never opens."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    stale = _meta(actor).model_copy(update={"compaction_due_at": datetime.now(UTC) - timedelta(hours=2)})
    actor.sm.store[META_KEY] = stale.model_dump_json()
    actor.armed.clear()

    await _read(actor, read)

    # Bound before the comparison: `_meta` RE-READS the record, so narrowing one call says nothing about
    # the next — and `ty` runs with `error-on-warning`, which makes that a build break rather than a nit.
    due = _meta(actor).compaction_due_at
    assert [name for name, _due, _period in actor.armed] == [COMPACTION_REMINDER]
    assert due is not None
    assert due > datetime.now(UTC)


@pytest.mark.asyncio
async def test_a_read_does_not_push_a_pending_reminder_forward() -> None:
    """Re-arming unconditionally would reset the due time on every interaction, and an inbox that is
    read often would then never be compacted at all — the repair turning into the leak."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    due_before = _meta(actor).compaction_due_at
    actor.armed.clear()

    await actor.unread()
    await actor.page({"limit": 10})

    assert actor.armed == []
    assert _meta(actor).compaction_due_at == due_before


@pytest.mark.asyncio
async def test_a_second_delivery_does_not_re_arm_a_pending_reminder() -> None:
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    await actor.deliver(_delivery("run-2"))
    assert len(actor.armed) == 1


@pytest.mark.asyncio
async def test_every_arming_uses_the_one_reminder_name() -> None:
    """`register_reminder` is an overwrite BY NAME, so however many times the read path repairs a lost
    reminder it can never leave two behind — which is what makes the repair safe to run on every call."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    for _ in range(3):
        stale = _meta(actor).model_copy(update={"compaction_due_at": datetime.now(UTC) - timedelta(hours=2)})
        actor.sm.store[META_KEY] = stale.model_dump_json()
        await actor.unread()

    # The names AND the Scheduler's own registry: three repairs, one entry. Asserting only the names
    # would still pass if each repair had appended a reminder under the same name.
    assert {name for name, _due, _period in actor.armed} == {COMPACTION_REMINDER}
    assert list(actor.reminders) == [COMPACTION_REMINDER]
    assert actor.unregistered == []


@pytest.mark.asyncio
async def test_an_empty_inbox_arms_nothing() -> None:
    actor = _Actor()
    await actor.unread()
    assert actor.armed == []


@pytest.mark.asyncio
async def test_a_foreign_reminder_is_ignored() -> None:
    actor = _Actor()
    await actor.deliver(_delivery("run-1", minutes_ago=48 * 60))
    saves = actor.sm.saves

    await actor.receive_reminder("lease", b"", timedelta(0), timedelta(seconds=60))

    assert actor.sm.saves == saves


@pytest.mark.asyncio
async def test_compaction_trims_past_the_retention_window_and_reconciles_the_count() -> None:
    actor = _Actor()
    await actor.deliver(_delivery("old", minutes_ago=48 * 60))  # older than the suite's 86400s TTL
    await actor.deliver(_delivery("new", minutes_ago=5))

    await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(0), timedelta(seconds=3600))

    assert [p.source_run_id for p in _rows(actor).pointers] == ["new"]
    assert (_meta(actor).unread, _meta(actor).rows) == (1, 1)


@pytest.mark.asyncio
async def test_compaction_caps_the_rows_at_the_configured_maximum() -> None:
    actor = _Actor()
    for n in range(8):  # the suite pins RASK_NOTIFICATIONS_INBOX_MAX_ROWS to 5
        await actor.deliver(_delivery(f"run-{n}", minutes_ago=n))

    await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(0), timedelta(seconds=3600))

    assert _meta(actor).rows == 5


@pytest.mark.asyncio
async def test_no_ttl_metadata_is_sent_while_the_feature_is_off() -> None:
    """The default, and the ONE thing that must not be "written anyway because it is free".

    daprd does not ignore a `ttlInSeconds` it cannot honour — it REFUSES the whole transactional
    upsert (`dapr/dapr` v1.18.1 `pkg/actors/api/transactional.go`: *ttlInSeconds is not supported
    without the "ActorStateTTL" feature enabled*), and `chart/` declares no `features:` block. Sending
    it unconditionally therefore fails EVERY write in this actor on the shipped release: no pointer is
    ever stored, every bus delivery RETRYs to the dead-letter topic, and each attempt leaves behind a
    compaction reminder that can never disarm because the disarm path also has to save.
    """
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    assert actor.sm.ttls == {ROWS_KEY: None, META_KEY: None}


@pytest.mark.asyncio
async def test_both_partitions_carry_the_ttl_backstop_once_the_feature_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt, never braces — but the belt is fastened only where the sidecar accepts it.

    Flipped in the SAME rollout that puts `ActorStateTTL` on the Dapr `Configuration` (a sidecar reads
    that at boot). On BOTH keys, so they can never expire apart into a count with no rows.
    """
    monkeypatch.setenv("RASK_NOTIFICATIONS_ACTOR_STATE_TTL_ENABLED", "true")
    get_notifications_settings.cache_clear()
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    assert actor.sm.ttls == {ROWS_KEY: 86400, META_KEY: 86400}


@pytest.mark.asyncio
async def test_both_partitions_are_written_in_one_save() -> None:
    """One transaction, so the count and the rows it was derived from can never be half-applied."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    assert actor.sm.saves == 1


@pytest.mark.asyncio
async def test_a_record_owned_by_another_subject_is_unreadable_not_absent() -> None:
    """The second lock. Reporting it absent would let THIS caller overwrite the other subject's rows;
    serving it would be the cross-subject leak the whole identity derivation exists to prevent."""
    actor = _Actor(SUBJECT)
    actor.sm.store[META_KEY] = InboxMeta(subject=OTHER, unread=3, rows=3, updated_at=datetime.now(UTC)).model_dump_json()

    with pytest.raises(InboxUnreadable) as refused:
        await actor.unread()
    assert refused.value.partition == META_KEY


@pytest.mark.asyncio
async def test_a_row_record_owned_by_another_subject_is_unreadable() -> None:
    actor = _Actor(SUBJECT)
    now = datetime.now(UTC)
    actor.sm.store[META_KEY] = InboxMeta(subject=SUBJECT, unread=0, rows=1, updated_at=now).model_dump_json()
    actor.sm.store[ROWS_KEY] = InboxRows(subject=OTHER, pointers=[], updated_at=now).model_dump_json()

    with pytest.raises(InboxUnreadable) as refused:
        await actor.page({"limit": 10})
    assert refused.value.partition == ROWS_KEY


@pytest.mark.asyncio
async def test_a_record_that_no_longer_fits_its_schema_is_unreadable_not_absent() -> None:
    """Schema drift is the OTHER way a record becomes unservable, and it is the one that shipped: a
    field changing type made an intact user-state row read as `exists: false`, and the client's next
    write destroyed it."""
    actor = _Actor()
    actor.sm.store[META_KEY] = json.dumps({"subject": SUBJECT, "unread": "lots"})

    with pytest.raises(InboxUnreadable):
        await actor.unread()


@pytest.mark.asyncio
async def test_compaction_skips_a_tick_it_cannot_read_rather_than_retrying_forever() -> None:
    """A repeating reminder that raises retries a permanent failure at the runtime's pace forever.
    Dropping THIS tick still leaves the next one to try at the compaction cadence."""
    actor = _Actor()
    actor.sm.store[META_KEY] = json.dumps({"subject": SUBJECT, "unread": "lots"})

    await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(0), timedelta(seconds=3600))

    assert actor.sm.saves == 0


@pytest.mark.asyncio
async def test_the_page_is_ordered_and_bounded_through_the_actor() -> None:
    actor = _Actor()
    for n in range(4):
        await actor.deliver(_delivery(f"run-{n}", minutes_ago=n))

    page = await actor.page({"limit": 2, "state": "all"})

    assert [p["source_run_id"] for p in page["pointers"]] == ["run-0", "run-1"]
    assert (page["has_more"], page["unread"]) == (True, 4)


@pytest.mark.asyncio
async def test_the_unread_filter_hides_rows_the_reader_has_dealt_with() -> None:
    actor = _Actor()
    await actor.deliver(_delivery("run-1", minutes_ago=1))
    await actor.deliver(_delivery("run-2", minutes_ago=2))
    await actor.mark_seen({"notification_ids": ["run-1@FAIL"]})

    page = await actor.page({"limit": 10, "state": "unread"})

    assert [p["source_run_id"] for p in page["pointers"]] == ["run-2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("deliver", _delivery("run-1")),
        ("page", {"limit": 10}),
        ("mark_seen", {"notification_ids": ["run-1@FAIL"]}),
        ("dismiss", {"notification_id": "run-1@FAIL"}),
    ],
)
async def test_the_actor_takes_no_subject_from_any_caller(method: str, payload: dict[str, Any]) -> None:
    """Identity is derived from the actor id — which the HTTP layer mints from the VERIFIED token sub —
    so there is no parameter anyone could forget to check. A payload that tries to state one is REFUSED
    rather than quietly ignored: silently dropping it would let a caller believe it had addressed
    somebody else's inbox and be told nothing.

    Every payload-taking method, not just the delivery one: `extra="forbid"` is what makes the refusal,
    and it is a per-model setting that a fifth model would have to opt into again."""
    actor = _Actor(SUBJECT)

    with pytest.raises(ValidationError):
        await getattr(actor, method)({**payload, "subject": OTHER})

    assert actor.sm.store == {}


@pytest.mark.asyncio
async def test_the_stored_records_carry_this_actors_own_subject() -> None:
    actor = _Actor(SUBJECT)
    await actor.deliver(_delivery("run-1"))
    assert (_meta(actor).subject, _rows(actor).subject) == (SUBJECT, SUBJECT)


# ---------------------------------------------------------------------------------------------------
# Boundaries, named: empty · under the limit · exactly the limit · one past it · only-dismissed · a row
# that is both seen AND dismissed. The pure rules are proved over lists in `test_feed.py`; what is
# proved here is that the actor's own doors reach them with the query it was handed.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivered", "served", "more"),
    [(0, 0, False), (2, 2, False), (3, 3, False), (4, 3, True)],
    ids=["empty", "under-the-limit", "exactly-the-limit", "one-past-the-limit"],
)
async def test_the_has_more_probe_turns_over_at_exactly_the_limit(delivered: int, served: int, more: bool) -> None:
    """`has_more` comes from a `limit + 1` read, so the only values that can distinguish a correct probe
    from a broken one are the three around the limit itself: a probe reading exactly `limit` reports
    "more" for a page that is in fact the last, and every caller then asks for an empty page."""
    actor = _Actor()
    for n in range(delivered):
        await actor.deliver(_delivery(f"run-{n}", minutes_ago=n))

    page = await actor.page({"limit": 3})

    assert (len(page["pointers"]), page["has_more"]) == (served, more)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["all", "unread"])
async def test_a_subject_whose_rows_are_all_dismissed_pages_empty(state: str) -> None:
    """Dismissed rows are invisible under BOTH filters, so an inbox that is entirely dismissed is
    indistinguishable from an empty one to the reader — while still holding its rows, which is what the
    next test depends on."""
    actor = _Actor()
    for run in ("run-1", "run-2"):
        await actor.deliver(_delivery(run))
        await actor.dismiss({"notification_id": notification_id(run, "FAIL")})

    page = await actor.page({"limit": 10, "state": state})

    assert (page["pointers"], page["has_more"], page["unread"]) == ([], False, 0)
    assert _meta(actor).rows == 2


@pytest.mark.asyncio
async def test_a_dismissed_row_still_dedupes_its_own_redelivery() -> None:
    """Dismissal hides a row; it does not delete it — and that is load-bearing rather than incidental.
    JetStream is at-least-once, so the same notification WILL be offered again; a dismissal that removed
    the row would let it arrive minutes later as a fresh unread one, which reads as the dismiss button
    not working."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    await actor.dismiss({"notification_id": "run-1@FAIL"})

    assert await actor.deliver(_delivery("run-1")) == {"delivered": False, "unread": 0, "rows": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sequence",
    [
        (("mark_seen", {"notification_ids": ["run-1@FAIL"]}), ("dismiss", {"notification_id": "run-1@FAIL"})),
        (("dismiss", {"notification_id": "run-1@FAIL"}), ("mark_seen", {"notification_ids": ["run-1@FAIL"]})),
    ],
    ids=["seen-then-dismissed", "dismissed-then-seen"],
)
async def test_a_row_that_is_both_seen_and_dismissed_leaves_the_count_at_zero(sequence: tuple[tuple[str, dict[str, Any]], ...]) -> None:
    """Unread is `neither seen nor dismissed`, so the two flags cannot subtract twice however they are
    applied. Both orders happen for real: a panel marks what it rendered as seen and the reader then
    dismisses one of those rows, and a reader can equally dismiss a row the panel had not yet marked."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))

    for method, payload in sequence:
        await getattr(actor, method)(payload)

    assert _recount(actor) == (0, 1)
    assert (_meta(actor).unread, _meta(actor).rows) == (0, 1)
    assert (await actor.page({"limit": 10, "state": "all"}))["pointers"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivered",
    [0, 1, 5, 6, 7],
    ids=["empty", "one", "under-a-full-page", "an-exact-multiple-of-the-page", "one-past-a-multiple"],
)
async def test_a_cursor_walk_covers_every_row_exactly_once(delivered: int) -> None:
    """The paging contract driven end to end rather than a page at a time: resume strictly after the row
    the cursor names, stop when the probe says there is no more. An off-by-one in either half surfaces
    here as a repeated or a missing notification — the two failures a reader actually notices — instead
    of as a `has_more` value nobody looks at.

    The exact-multiple case is the one that breaks naive implementations: a walk that stops on an empty
    page rather than on the probe serves the last page twice."""
    actor = _Actor()
    for n in range(delivered):
        await actor.deliver(_delivery(f"run-{n}", minutes_ago=n))

    served: list[str] = []
    query: dict[str, Any] = {"limit": 2, "state": "all"}
    # Bounded so a walk that never terminates fails as a wrong row count rather than hanging the suite.
    for _ in range(delivered + 2):
        page = await actor.page(query)
        served.extend(pointer["notification_id"] for pointer in page["pointers"])
        if not page["has_more"]:
            break
        last = page["pointers"][-1]
        query = {**query, "after": {"occurred_at": last["occurred_at"], "notification_id": last["notification_id"]}}

    assert served == [notification_id(f"run-{n}", "FAIL") for n in range(delivered)]


# ---------------------------------------------------------------------------------------------------
# The count never races itself. Turn-based concurrency serialises the calls; what makes the ANSWER right
# is that every mutation re-derives the count from the rows. The two tests below are a pair: the first
# states the property, the second drives the same sequence through the counter-adjusting form this actor
# deliberately does not use, so the first is a claim about the design rather than about arithmetic.
# ---------------------------------------------------------------------------------------------------


#: Ten steps against ONE inbox, containing the two an adjusted counter gets wrong: a REPLAYED delivery
#: (at-least-once, or the reconciler re-offering a run the bus already delivered) and a dismiss of a row
#: the reader had already marked seen. Without those two, "these differ" would be true of almost any two
#: sequences and the drift proof would gate nothing.
_INTERLEAVING: tuple[tuple[str, str], ...] = (
    ("deliver", "run-1"),
    ("deliver", "run-2"),
    ("mark", "run-1"),
    ("deliver", "run-1"),
    ("dismiss", "run-1"),
    ("deliver", "run-3"),
    ("mark", "run-2"),
    ("mark", "run-2"),
    ("dismiss", "run-3"),
    ("deliver", "run-4"),
)


async def _step(actor: _Actor, op: str, run: str) -> dict[str, Any]:
    if op == "deliver":
        return await actor.deliver(_delivery(run))
    if op == "mark":
        return await actor.mark_seen({"notification_ids": [notification_id(run, "FAIL")]})
    return await actor.dismiss({"notification_id": notification_id(run, "FAIL")})


def _counter_adjusted_unread() -> int:
    """The form this actor does not use: ONE stored number, `+1` on a delivery and `-1` on a read or a
    dismiss. Driven over the same sequence so the property above is shown to be doing work."""
    return sum(1 if op == "deliver" else -1 for op, _run in _INTERLEAVING)


@pytest.mark.asyncio
async def test_the_count_never_drifts_from_the_rows_across_an_interleaved_sequence() -> None:
    """After EVERY step, three numbers agree: what the call answered, what the meta partition stores,
    and a recount of the rows themselves. The third is the point — reading the count back out of the
    same partition that produced it would prove nothing about whether it is derived."""
    actor = _Actor()

    for op, run in _INTERLEAVING:
        answer = await _step(actor, op, run)
        assert (answer["unread"], answer["rows"]) == _recount(actor), f"{op} {run} answered a count the rows do not support"
        assert (_meta(actor).unread, _meta(actor).rows) == _recount(actor), f"{op} {run} stored a count the rows do not support"

    assert _recount(actor) == (1, 4)


@pytest.mark.asyncio
async def test_a_counter_that_is_adjusted_rather_than_derived_drifts_on_the_same_sequence() -> None:
    """The regression proof for the test above. Two ordinary steps break the adjusted form — a
    redelivery of a run already in the inbox (`+1` for a row that was not added) and a dismiss of a row
    already marked seen (`-1` for a row that had already left the count) — and it ends one below the
    truth, which as a badge means an unread notification nobody is ever told about."""
    actor = _Actor()
    for op, run in _INTERLEAVING:
        await _step(actor, op, run)

    derived, _rows_stored = _recount(actor)
    assert _counter_adjusted_unread() != derived
    assert _meta(actor).unread == derived


# ---------------------------------------------------------------------------------------------------
# Reminder ordering under failure. The Scheduler's etcd and the state store fail independently, so the
# ordering is only load-bearing at the moment one of them refuses — which is the moment the tests above
# never reach.
# ---------------------------------------------------------------------------------------------------


class _ArmsAfterPersisting(_Actor):
    """The OLD ordering — persist first, arm second — reproduced so the rule can be shown to be doing
    work. `19677ff` fixed exactly this in the project actor. It is a helper rather than a comment
    because "the order matters here" is the kind of claim a suite can carry for years unexercised.
    """

    @override
    async def _persist(self, pointers: list[InboxPointer], meta: InboxMeta | None) -> dict[str, Any]:
        now = datetime.now(UTC)
        subject = self._subject()
        unread = unread_count(pointers)
        await self._save(
            InboxMeta(subject=subject, unread=unread, rows=len(pointers), compaction_due_at=None, updated_at=now),
            rows=InboxRows(subject=subject, pointers=pointers, updated_at=now),
        )
        if pointers:
            await self._arm_compaction(None, now)
        return {"unread": unread, "rows": len(pointers)}


@pytest.mark.asyncio
async def test_a_store_failure_mid_transition_leaves_the_safety_reminder_armed() -> None:
    """The benign half of the ordering, asserted rather than assumed: a reminder armed for rows that
    then fail to persist wakes a pod to find nothing to do and stands down. Nothing was written, and the
    inbox is left with a bound it does not yet need rather than rows with no bound at all."""
    actor = _Actor()
    actor.sm.save_error = RuntimeError("lance-statestore refused the transaction")

    with pytest.raises(RuntimeError, match="lance-statestore"):
        await actor.deliver(_delivery("run-1"))

    assert list(actor.reminders) == [COMPACTION_REMINDER]
    assert actor.sm.store == {}
    assert actor.unregistered == []


@pytest.mark.asyncio
async def test_a_scheduler_that_refuses_the_arming_persists_no_rows_at_all() -> None:
    """Arming first also makes a Scheduler outage a clean refusal: the delivery fails, JetStream
    redelivers it, and no row is left behind that nothing is scheduled to trim."""
    actor = _Actor()
    actor.register_error = RuntimeError("dapr-scheduler-server is unreachable")

    with pytest.raises(RuntimeError, match="scheduler"):
        await actor.deliver(_delivery("run-1"))

    assert actor.sm.store == {}
    assert actor.reminders == {}


@pytest.mark.asyncio
async def test_arming_after_the_persist_would_leave_the_rows_with_no_bound() -> None:
    """The same Scheduler outage against the same actor with only the two steps swapped: the rows commit
    and the arming then fails, leaving an inbox nothing will ever trim. `ActorStateTTL` is off on this
    estate, so there is no floor underneath — which is why the ordering, not the retry, is the fix."""
    actor = _ArmsAfterPersisting()
    actor.register_error = RuntimeError("dapr-scheduler-server is unreachable")

    with pytest.raises(RuntimeError, match="scheduler"):
        await actor.deliver(_delivery("run-1"))

    assert _rows(actor).pointers != []
    assert actor.reminders == {}


@pytest.mark.asyncio
async def test_the_disarm_never_runs_before_the_save_that_empties_the_inbox() -> None:
    """Disarm late, for the mirror reason: unregistering first and then failing to persist the empty row
    set would leave the rows in place with their only bound already gone."""
    actor = _Actor()
    await actor.deliver(_delivery("old", minutes_ago=48 * 60))
    actor.sm.save_error = RuntimeError("lance-statestore refused the transaction")

    with pytest.raises(RuntimeError, match="lance-statestore"):
        await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(0), timedelta(seconds=3600))

    assert actor.unregistered == []
    assert list(actor.reminders) == [COMPACTION_REMINDER]


@pytest.mark.asyncio
async def test_the_compaction_reminder_repeats_at_the_configured_interval() -> None:
    """Due time AND period. A one-shot reminder (period 0) bounds an inbox exactly once and then never
    again, and the difference is invisible until a month of rows has accumulated behind it."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    assert actor.armed == [(COMPACTION_REMINDER, 3600.0, 3600.0)]


@pytest.mark.asyncio
async def test_a_scheduler_that_refuses_the_disarm_still_empties_the_inbox() -> None:
    """The disarm is best-effort by design — an inbox can legitimately be emptied twice, so an absent or
    unreachable reminder must not turn a successful compaction into a failed one and leave the rows it
    just trimmed unpersisted."""
    actor = _Actor()
    await actor.deliver(_delivery("old", minutes_ago=48 * 60))
    actor.unregister_error = RuntimeError("dapr-scheduler-server is unreachable")

    await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(0), timedelta(seconds=3600))

    assert _rows(actor).pointers == []
    assert (_meta(actor).unread, _meta(actor).rows) == (0, 0)


@pytest.mark.asyncio
async def test_an_inbox_whose_meta_records_no_due_time_is_armed_on_the_next_read() -> None:
    """`compaction_due_at` is the only thing that tells "armed and pending" from "armed once, and the
    Scheduler lost it". A record naming no due time at all is the second case — an arming that never
    landed — and must be repaired rather than read as "no reminder is wanted here"."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    actor.sm.store[META_KEY] = _meta(actor).model_copy(update={"compaction_due_at": None}).model_dump_json()
    actor.armed.clear()

    await actor.unread()

    assert [name for name, _due, _period in actor.armed] == [COMPACTION_REMINDER]
    assert _meta(actor).compaction_due_at is not None


@pytest.mark.asyncio
async def test_a_meta_record_reporting_no_rows_arms_nothing() -> None:
    """The repair is conditional on there being something to bound. An inbox emptied by compaction keeps
    its meta record, and re-arming for zero rows would wake a pod on a schedule for an inbox with
    nothing in it — a reminder the read path would then keep alive forever."""
    actor = _Actor()
    actor.sm.store[META_KEY] = InboxMeta(subject=SUBJECT, unread=0, rows=0, updated_at=datetime.now(UTC)).model_dump_json()

    await actor.unread()

    assert actor.armed == []


@pytest.mark.asyncio
async def test_a_repaired_reminder_is_persisted_so_the_next_read_repeats_nothing() -> None:
    """The repair writes the new due time back. Without that write every later badge poll would read the
    same stale record and re-arm again — a repair that never converges, on the most frequent call in the
    plane, against the one component whose outage it exists to survive."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    stale = _meta(actor).model_copy(update={"compaction_due_at": datetime.now(UTC) - timedelta(hours=2)})
    actor.sm.store[META_KEY] = stale.model_dump_json()

    await actor.unread()
    actor.armed.clear()
    saves = actor.sm.saves
    await actor.unread()

    assert actor.armed == []
    assert actor.sm.saves == saves


@pytest.mark.asyncio
async def test_a_compaction_tick_moves_the_recorded_due_time_forward() -> None:
    """A tick that left the recorded due time in the past would make every read after it believe the
    reminder had been lost and re-arm it — the repair firing against a reminder that is working."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1", minutes_ago=5))
    overdue = _meta(actor).model_copy(update={"compaction_due_at": datetime.now(UTC) - timedelta(seconds=1)})
    actor.sm.store[META_KEY] = overdue.model_dump_json()

    await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(0), timedelta(seconds=3600))

    due = _meta(actor).compaction_due_at
    assert due is not None
    assert due > datetime.now(UTC)


# ---------------------------------------------------------------------------------------------------
# The subject second lock, on the paths where reporting a foreign record as ABSENT would destroy it.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("deliver", _delivery("run-1")),
        ("mark_seen", {"notification_ids": ["run-1@FAIL"]}),
        ("dismiss", {"notification_id": "run-1@FAIL"}),
    ],
)
async def test_a_write_path_refuses_another_subjects_record_without_overwriting_it(method: str, payload: dict[str, Any]) -> None:
    """Absent-versus-unreadable on the paths where the difference bites. Read as absent, a foreign record
    is an empty inbox — and the very next write replaces somebody else's rows with this subject's. That
    is the failure the secret plane shipped twice, so it is asserted on every mutating door, not one."""
    actor = _Actor(SUBJECT)
    foreign = InboxMeta(subject=OTHER, unread=3, rows=3, updated_at=datetime.now(UTC)).model_dump_json()
    actor.sm.store[META_KEY] = foreign

    with pytest.raises(InboxUnreadable) as refused:
        await getattr(actor, method)(payload)

    assert refused.value.partition == META_KEY
    assert actor.sm.store[META_KEY] == foreign


@pytest.mark.asyncio
async def test_a_foreign_rows_record_is_refused_before_the_write_that_would_replace_it() -> None:
    """The rows partition needs its own check: a meta record this subject owns says nothing about who
    owns the rows beside it, and the rows are the partition a delivery rewrites wholesale."""
    actor = _Actor(SUBJECT)
    now = datetime.now(UTC)
    actor.sm.store[META_KEY] = InboxMeta(subject=SUBJECT, unread=0, rows=1, updated_at=now).model_dump_json()
    foreign = InboxRows(subject=OTHER, pointers=[], updated_at=now).model_dump_json()
    actor.sm.store[ROWS_KEY] = foreign

    with pytest.raises(InboxUnreadable) as refused:
        await actor.deliver(_delivery("run-1"))

    assert refused.value.partition == ROWS_KEY
    assert actor.sm.store[ROWS_KEY] == foreign


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_id", ["a", "__4"], ids=["not-base64", "not-utf8"])
async def test_an_actor_id_that_is_not_an_encoded_subject_is_unreadable_not_absent(actor_id: str) -> None:
    """The id IS the identity, so an id this actor cannot decode is an inbox whose owner it cannot name.
    Answering "empty" would invite the caller to write rows under a key nobody can attribute to a person
    — the same destroy-on-next-write shape as a foreign record, reached from the other side."""
    actor = _actor_with_id(actor_id)

    with pytest.raises(InboxUnreadable) as refused:
        await actor.deliver(_delivery("run-1"))

    assert refused.value.partition == "actor-id"
    assert actor.sm.store == {}


@pytest.mark.asyncio
async def test_compaction_refuses_a_tick_for_another_subjects_record_rather_than_trimming_it() -> None:
    """The reminder fires on a schedule with no caller to refuse, so the ownership check has to hold on
    this path too — otherwise a mis-keyed row is rewritten by the one caller that never looked at it."""
    actor = _Actor(SUBJECT)
    foreign = InboxMeta(subject=OTHER, unread=3, rows=3, updated_at=datetime.now(UTC)).model_dump_json()
    actor.sm.store[META_KEY] = foreign

    await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(0), timedelta(seconds=3600))

    assert actor.sm.saves == 0
    assert actor.sm.store[META_KEY] == foreign


def test_no_actor_method_declares_a_subject_parameter() -> None:
    """Identity is derived, and the way it stays derived is that there is nowhere to state it. The module
    header claims this in prose; this is what keeps it true when a sixth method is added."""
    declared = {name: member for name, member in vars(InboxActorInterface).items() if hasattr(member, "__actormethod__")}
    assert declared, "the interface declares no actor methods"

    offenders = {name: list(inspect.signature(member).parameters) for name, member in declared.items()}
    assert {name: params for name, params in offenders.items() if params not in (["self"], ["self", "payload"])} == {}


# ---------------------------------------------------------------------------------------------------
# Two partitions, one transaction — and what the actor does when it nonetheless finds only one of them.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lost_rows_partition_is_reconciled_by_the_next_mutation() -> None:
    """The count is derived, never trusted — including when the rows it was derived from are gone. Both
    partitions move in one transaction and carry the same TTL precisely so this cannot arise in the
    normal course; if it does, the next mutation makes the count agree with what is actually there
    rather than carrying a badge for rows nobody can serve."""
    actor = _Actor()
    for run in ("run-1", "run-2", "run-3"):
        await actor.deliver(_delivery(run))
    del actor.sm.store[ROWS_KEY]

    await actor.deliver(_delivery("run-4"))

    assert (_meta(actor).unread, _meta(actor).rows) == (1, 1)
    assert _recount(actor) == (1, 1)


@pytest.mark.asyncio
async def test_a_meta_partition_lost_on_its_own_is_rebuilt_from_the_rows_that_remain() -> None:
    """The mirror case: the small partition is a derivation, so losing it loses nothing that cannot be
    recomputed from the large one."""
    actor = _Actor()
    for run in ("run-1", "run-2"):
        await actor.deliver(_delivery(run))
    del actor.sm.store[META_KEY]

    await actor.deliver(_delivery("run-3"))

    assert (_meta(actor).unread, _meta(actor).rows) == (3, 3)


# ---------------------------------------------------------------------------------------------------
# One producer's clock cannot take paging down for the subject who received it.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_naive_instant_from_one_producer_cannot_take_paging_down() -> None:
    """A producer stamping a naive timestamp is not hypothetical — OpenLineage `eventTime` arrives from
    whatever emitted it. Normalising at the boundary is what keeps the feed's order total."""
    actor = _Actor()
    await actor.deliver({**_delivery("aware"), "occurred_at": "2026-08-09T12:00:00+00:00"})
    await actor.deliver({**_delivery("naive"), "occurred_at": "2026-08-09T13:00:00"})

    page = await actor.page({"limit": 10, "state": "all"})

    assert [pointer["source_run_id"] for pointer in page["pointers"]] == ["naive", "aware"]


def test_comparing_a_naive_instant_to_an_aware_one_raises_rather_than_mis_sorting() -> None:
    """Why the normalisation above is a fix and not a nicety: un-normalised, the two instants are not
    comparable at all, so one such row 500s every page for whoever received it — it does not merely land
    in the wrong place."""
    with pytest.raises(TypeError):
        _ = sorted([datetime(2026, 8, 9, 12, 0, tzinfo=UTC), datetime(2026, 8, 9, 13, 0)])
