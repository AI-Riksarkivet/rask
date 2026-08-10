"""The inbox under attack: the store, the actor id, and the read plane.

`test_adversarial_ingress.py` drives the bus from the publisher's side. This suite drives everything
BEHIND it — the actor's state, the id it is addressed by, and the door that reads it back — from the
side a caller (or a producer who has already got a pointer written) can reach.

Three kinds of test live here and they are not interchangeable, so each says which it is in its own
name and first line:

* **A guarantee.** The plane claims it; the test would fail if it stopped being true.
* **A gap, pinned.** The plane does NOT do this, the omission is a decision or an unclosed edge, and
  the test passes TODAY by asserting the gap. It is named for the gap so that closing it turns the
  suite red and the test is rewritten rather than quietly outliving its subject.
* **A dependency on the runtime.** The behaviour is correct only because Dapr serialises turns against
  one actor id. Those tests deliberately break the serialisation and show what is lost — which is the
  only way a suite with no sidecar in it can state what the sidecar is holding up.

Nothing here needs a sidecar, a placement service or a database, and that is also this suite's limit:
it can prove what happens when the lock is absent and cannot prove that the lock is there.
"""

import asyncio
import base64
import json
import logging
import time
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from dapr.actor import ActorId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import inbox as inbox_module
from notifications.api import security as security_module
from notifications.api.ingest import DAPR_DROP, ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.visibility import Visibility
from notifications.config import get_notifications_settings
from notifications.errors import InboxUnreadable
from notifications.feed import paginate, unread_count
from notifications.inbox_actor import COMPACTION_REMINDER, META_KEY, ROWS_KEY, InboxActor
from notifications.models import InboxDismiss, InboxMark, InboxMeta, InboxPointer, InboxQuery, InboxRows, NotificationReason, notification_id
from notifications.proxies import TypedActorProxy, _translating, inbox_actor_id, inbox_for
from service_kit.exceptions import register_handlers
from service_kit.governed.user_state import decode_subject


SUBJECT = "alice"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

#: The whole base64url alphabet — needed to enumerate every id that decodes to one subject.
_BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


# ── the actor, driven against a state manager that can be made to interleave ────────────────────────


class _GatedStateManager:
    """The actor's staged-then-flushed state, with one turn suspendable mid-read.

    `try_get_state` computes its answer BEFORE it suspends, which is the whole point: a turn that
    suspended before reading would resume and read the OTHER turn's committed value, and the
    read-modify-write window this suite is about would never open. What is modelled is the real
    sequence — read the rows over the sidecar hop, decide, write back — with the hop made to take long
    enough that a second turn finishes inside it.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.staged: dict[str, str] = {}
        self.gate: asyncio.Event | None = None
        self.gate_key: str | None = None

    def suspend_next_read_of(self, key: str) -> asyncio.Event:
        """Arm the gate: the NEXT read of `key` answers from the store as it is now, then waits."""
        gate = asyncio.Event()
        self.gate, self.gate_key = gate, key
        return gate

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        answer = (True, self.staged[key]) if key in self.staged else (key in self.store, self.store.get(key))
        if self.gate is not None and key == self.gate_key:
            gate, self.gate = self.gate, None
            await gate.wait()
        return answer

    async def set_state_ttl(self, key: str, value: str, ttl_in_seconds: int | None) -> None:
        self.staged[key] = value

    async def save_state(self) -> None:
        self.store.update(self.staged)
        self.staged.clear()


class _Actor(InboxActor):
    """The real actor with its Dapr plumbing replaced — state in memory, reminders recorded."""

    def __init__(self, subject: str = SUBJECT, *, actor_id: str | None = None) -> None:
        self.sm = _GatedStateManager()
        self._state_manager = cast(Any, self.sm)
        self.id = ActorId(actor_id if actor_id is not None else inbox_actor_id(subject))
        self.reminders: dict[str, tuple[float, float]] = {}

    async def register_reminder(
        self,
        name: str,
        state: bytes,
        due_time: timedelta,
        period: timedelta | None = None,
        ttl: timedelta | None = None,
        failure_policy: Any = None,
    ) -> None:
        self.reminders[name] = (due_time.total_seconds(), (period or timedelta(0)).total_seconds())

    async def unregister_reminder(self, name: str) -> None:
        self.reminders.pop(name, None)


def _delivery(run: str, *, state: str = "FAIL", at: datetime | None = None) -> dict[str, Any]:
    return {
        "notification_id": notification_id(run, state),
        "reason": NotificationReason.AUTHOR.value,
        "object_id": "silver$pages",
        "source_run_id": run,
        "occurred_at": (at if at is not None else datetime.now(UTC)).isoformat(),
    }


def _stored_ids(actor: _Actor) -> list[str]:
    return [pointer.notification_id for pointer in InboxRows.model_validate_json(actor.sm.store[ROWS_KEY]).pointers]


def _stored_meta(actor: _Actor) -> InboxMeta:
    return InboxMeta.model_validate_json(actor.sm.store[META_KEY])


async def _compaction_tick(actor: _Actor) -> None:
    await actor.receive_reminder(COMPACTION_REMINDER, b"", timedelta(hours=1), timedelta(hours=1))


# ── the door, driven against an inbox double ────────────────────────────────────────────────────────


def _pointer(index: int, *, obj: str = "silver$pages") -> InboxPointer:
    return InboxPointer(
        notification_id=f"run-{index:03d}@FAIL",
        reason=NotificationReason.AUTHOR,
        object_id=obj,
        source_run_id=f"ingest-{index}",
        occurred_at=NOW - timedelta(minutes=index),
    )


class _Inbox:
    """One subject's inbox, answering with the actor's own pure rules — or with an injected fault."""

    def __init__(self, rows: list[InboxPointer] | None = None, *, fault: Exception | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.fault = fault
        self.seen_payloads: list[dict[str, Any]] = []

    def _raise_if_faulty(self) -> None:
        if self.fault is not None:
            raise self.fault

    async def page(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._raise_if_faulty()
        return paginate(self.rows, InboxQuery.model_validate(payload)).model_dump(mode="json")

    async def unread(self) -> dict[str, Any]:
        self._raise_if_faulty()
        return {"unread": unread_count(self.rows), "rows": len(self.rows)}

    async def mark_seen(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_payloads.append(payload)
        self._raise_if_faulty()
        wanted = set(InboxMark.model_validate(payload).notification_ids)
        updated = [row.marked_seen() if row.notification_id in wanted and not row.seen else row for row in self.rows]
        changed = sum(1 for before, after in zip(self.rows, updated, strict=True) if before is not after)
        self.rows = updated
        return {"updated": changed, "unread": unread_count(self.rows), "rows": len(self.rows)}

    async def dismiss(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._raise_if_faulty()
        target = InboxDismiss.model_validate(payload).notification_id
        updated = [row.marked_dismissed() if row.notification_id == target and not row.dismissed else row for row in self.rows]
        changed = sum(1 for before, after in zip(self.rows, updated, strict=True) if before is not after)
        self.rows = updated
        return {"dismissed": changed, "unread": unread_count(self.rows), "rows": len(self.rows)}


@pytest.fixture
def inbox() -> _Inbox:
    return _Inbox()


def _door(inbox: _Inbox, monkeypatch: pytest.MonkeyPatch, *, subject: str = SUBJECT) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(inbox_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.actors_registered = True
    app.state.fga = None
    app.dependency_overrides[security_module._deps.current_subject] = lambda: subject
    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, inbox))
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, inbox: _Inbox) -> Iterator[TestClient]:
    # `raise_server_exceptions=False` so an UNHANDLED error is observed as the status a browser would
    # receive. Re-raising it into the test would hide exactly the question these tests ask: what does
    # this door answer when something below it breaks in a way nobody wrote a handler for.
    with TestClient(_door(inbox, monkeypatch), raise_server_exceptions=False) as test_client:
        yield test_client


def _hide(app: FastAPI, hidden: str) -> None:
    """Make one object invisible to the page filter, through the door's own visibility dependency."""

    class _Filtered(Visibility):
        async def visible(self, subject: str, names: Any) -> set[str]:
            return {name for name in names if name != hidden}

    app.dependency_overrides[security_module.get_visibility] = lambda: _Filtered(client=None, enabled=True)


# ── who an actor id names: the second lock is not total ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("subject", "shadows"),
    [
        ("bob", 0),  # 3 bytes — the encoding is exact, so the id is the only one that means `bob`
        ("alice", 3),  # 5 bytes — the last character carries 2 unused bits
        ("auth0|5f3c", 15),  # 10 bytes — 4 unused bits, and the shape a real IdP `sub` actually has
    ],
)
def test_an_actor_id_is_not_canonical_so_several_ids_decode_to_one_subject(subject: str, shadows: int) -> None:
    """GAP, pinned. `decode_subject` is lenient about base64's unused trailing bits.

    `encode_subject` yields ONE id per subject, but its inverse accepts several: any subject whose byte
    length is not a multiple of three leaves 2 or 4 bits of slack in the final character, and Python's
    decoder ignores them rather than refusing. So the map from id to subject is many-to-one, and
    `InboxActor._subject()` — which decodes whatever the sidecar routed on — cannot tell an id this
    service minted from one that merely decodes to the same person.

    Rewrite when `decode_subject` rejects an id that does not round-trip (`encode_subject(decode(x)) == x`).
    """
    canonical = inbox_actor_id(subject)
    decoding_to_the_same_subject = [candidate for candidate in (canonical[:-1] + char for char in _BASE64URL) if _decodes_to(candidate, subject)]

    assert canonical in decoding_to_the_same_subject
    assert len(decoding_to_the_same_subject) == shadows + 1


def _decodes_to(candidate: str, subject: str) -> bool:
    try:
        return decode_subject(candidate) == subject
    except (ValueError, UnicodeDecodeError):
        return False


@pytest.mark.asyncio
async def test_the_second_lock_passes_for_a_record_under_an_id_this_service_could_not_have_minted() -> None:
    """GAP, pinned — the consequence of the test above, and the reason it matters.

    `_require_owner` compares a record's stored `subject` against the one decoded from the actor id, and
    the docstring calls that the SECOND lock: the thing that catches a mis-keyed row. It does catch a row
    belonging to somebody else. It does not catch a row under a non-canonical spelling of the right
    subject — that reads and writes exactly like the real inbox while being a different state partition,
    so a pointer written there is one the door will never show and nothing will ever count.

    Only reachable by addressing the actor plane directly (nothing in this service builds an id except
    `inbox_actor_id`), which is why it is a gap rather than a defect — and why the positive half is
    asserted with it: the door's own path mints the canonical id and no other.
    """
    canonical = inbox_actor_id(SUBJECT)
    shadow = next(candidate for candidate in (canonical[:-1] + char for char in _BASE64URL) if candidate != canonical and _decodes_to(candidate, SUBJECT))

    actor = _Actor(actor_id=shadow)
    result = await actor.deliver(_delivery("run-1"))

    assert result["delivered"] is True
    assert _stored_meta(actor).subject == SUBJECT
    assert inbox_actor_id(SUBJECT) == canonical != shadow


@pytest.mark.parametrize(
    ("label", "left", "right"),
    [
        ("two unicode normalisations of one name", unicodedata.normalize("NFC", "josé"), unicodedata.normalize("NFD", "josé")),
        ("a subject with surrounding whitespace", "alice", " alice"),
        ("a subject in another case", "alice", "Alice"),
    ],
)
def test_two_spellings_of_one_person_are_two_inboxes(label: str, left: str, right: str) -> None:
    """GAP, pinned. The subject is used byte-exactly and is never canonicalised.

    That is the SAFE direction — two distinct subjects can never collide on one inbox, which is the
    property that actually matters — and it costs the other one: an issuer that ever emits the same
    person's `sub` in two normalisations, or with stray whitespace, splits their notifications across
    two inboxes and neither badge is right. The estate's `sub` is opaque and stable, so this is a bound
    on the identity provider rather than a bug here; pinned because "we normalise" and "we don't" are
    both defensible and only one of them is true.

    Rewrite if `inbox_actor_id` ever normalises — the assertion inverts and the collision risk becomes
    the thing to test instead.
    """
    assert left != right, label
    assert inbox_actor_id(left) != inbox_actor_id(right)


@pytest.mark.parametrize("subject", ["", "   "])
def test_a_verified_token_with_an_unusable_subject_crashes_the_door_instead_of_refusing_it(subject: str) -> None:
    """GAP, pinned. `IDToken.sub` is a bare `str` with no `min_length`, so a conformant-looking token can
    carry an empty (or whitespace-only) one, and `current_subject` hands it straight on.

    `inbox_actor_id` then does the right thing — there is no anonymous inbox, so it raises — but nothing
    catches a `ValueError` on this path: `register_handlers` maps `DomainError` and `RequestValidationError`
    and there is no catch-all. The caller receives a bare 500 with no problem+json body, which is the one
    shape this plane's refusals are supposed never to take.

    The REAL `inbox_for` has to run for this, so the actor plane is deliberately not patched out — the
    refusal happens while composing the id, before any sidecar channel is opened.

    Rewrite when the door refuses an unusable subject by name.
    """
    app = FastAPI()
    register_handlers(app)
    app.include_router(inbox_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.actors_registered = True
    app.state.fga = None
    app.dependency_overrides[security_module._deps.current_subject] = lambda: subject

    with TestClient(app, raise_server_exceptions=False) as door:
        response = door.get("/notifications/inbox")

    assert response.status_code == 500
    assert not response.headers["content-type"].startswith("application/problem+json")


# ── the cursor discloses the row the filter just removed ────────────────────────────────────────────


def test_the_cursor_of_a_page_names_a_row_the_visibility_filter_removed(client: TestClient, inbox: _Inbox) -> None:
    """GAP, pinned, and the sharpest one on the read plane.

    `next_cursor` is built from the last RAW row of the window rather than the last VISIBLE one, on
    purpose: a cursor over the visible rows is `null` on a fully-hidden page and the reader stops at a
    wall with more of their own rows below it (`test_a_page_emptied_by_the_filter_still_hands_back_a_cursor`).

    The cost of that choice is stated nowhere and is real: the cursor is base64 of the ordering key, not
    a secret, so it hands the client the `notification_id` — hence the graph run id and the terminal
    state — and the exact instant of a row the same response just refused to show them. Narrow, because
    the row is in their OWN inbox and was visible to them when it was delivered; a disclosure all the
    same, and the one place where "an opaque cursor" is doing security work it was not built for.

    Rewrite when the cursor is opaque in fact (signed or server-side) rather than merely by convention.
    """
    inbox.rows = [_pointer(1, obj="gold$revoked"), _pointer(2), _pointer(3)]
    _hide(cast(FastAPI, client.app), "gold$revoked")

    body = client.get("/notifications/inbox", params={"limit": 1}).json()
    raw = body["next_cursor"]
    disclosed = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))

    assert body["notifications"] == []
    assert disclosed["notification_id"] == "run-001@FAIL"
    assert disclosed["occurred_at"].startswith("2026-08-09T11:59")


def test_a_page_that_shows_nothing_still_reports_the_hidden_rows_in_its_count(client: TestClient, inbox: _Inbox) -> None:
    """GAP, pinned. The badge is documented as unfiltered on `GET /inbox/unread`, where the reason is
    cost — filtering it would read every row on every poll. `GET /inbox` carries the same number without
    saying so, and there it is not free-of-charge but free-of-mention: a reader whose grants were all
    revoked sees an empty panel over a badge of three.

    It discloses no object, which is what keeps it a bound rather than a leak. Rewrite when the page's
    own count is derived from the rows the page actually served.
    """
    inbox.rows = [_pointer(1, obj="gold$revoked"), _pointer(2, obj="gold$revoked")]
    _hide(cast(FastAPI, client.app), "gold$revoked")

    body = client.get("/notifications/inbox").json()

    assert body["notifications"] == []
    assert body["unread"] == 2


# ── the runtime is the lock: what is lost the moment two turns overlap ──────────────────────────────


@pytest.mark.asyncio
async def test_two_overlapping_delivery_turns_lose_one_pointer() -> None:
    """A DEPENDENCY ON THE RUNTIME, made visible.

    The actor holds no etag and no optimistic concurrency, by decision: Dapr routes one actor id to one
    pod and serialises calls to it, so turn-based concurrency IS the lock. This drives the same two
    deliveries with the lock removed — the second turn runs to completion inside the first turn's
    read-modify-write — and the whole row set is written back from the stale read.

    The loss is SILENT, which is the part worth pinning: the lost delivery answered `delivered: True`,
    and the count is derived from the rows that survived, so nothing afterwards disagrees with anything.
    There is no drift to detect and no repair path. Only the sidecar's single activation prevents it,
    and no test in this service can prove that it is there.
    """
    actor = _Actor()
    gate = actor.sm.suspend_next_read_of(ROWS_KEY)

    slow = asyncio.create_task(actor.deliver(_delivery("first")))
    await asyncio.sleep(0)
    inner = await actor.deliver(_delivery("second"))
    gate.set()
    await slow

    assert inner == {"delivered": True, "unread": 1, "rows": 1}
    assert _stored_ids(actor) == ["first@FAIL"]
    assert (_stored_meta(actor).unread, _stored_meta(actor).rows) == (1, 1)


@pytest.mark.asyncio
async def test_a_delivery_landing_inside_a_compaction_tick_is_erased_by_it() -> None:
    """A DEPENDENCY ON THE RUNTIME. The reminder is an actor turn like any other, so the same window
    opens between the tick's read and its write — and this one is worse than a lost race between two
    deliveries, because compaction writes the WHOLE surviving set: everything that arrived while it was
    thinking is gone, not merely the one row that lost."""
    actor = _Actor()
    await actor.deliver(_delivery("old", at=datetime.now(UTC) - timedelta(hours=1)))
    gate = actor.sm.suspend_next_read_of(ROWS_KEY)

    tick = asyncio.create_task(_compaction_tick(actor))
    await asyncio.sleep(0)
    await actor.deliver(_delivery("fresh"))
    gate.set()
    await tick

    assert _stored_ids(actor) == ["old@FAIL"]


@pytest.mark.asyncio
async def test_a_delivery_landing_inside_a_mark_seen_turn_is_erased_by_it() -> None:
    """A DEPENDENCY ON THE RUNTIME, on the path a user drives themselves: the panel marking rows read is
    the most frequent write in the plane, and it rewrites the row set from its own read."""
    actor = _Actor()
    await actor.deliver(_delivery("one"))
    gate = actor.sm.suspend_next_read_of(ROWS_KEY)

    marking = asyncio.create_task(actor.mark_seen({"notification_ids": ["one@FAIL"]}))
    await asyncio.sleep(0)
    await actor.deliver(_delivery("two"))
    gate.set()
    await marking

    assert _stored_ids(actor) == ["one@FAIL"]


@pytest.mark.asyncio
async def test_the_same_sequences_run_as_turns_keep_every_row() -> None:
    """The control the three tests above need to mean anything: run in sequence — which is what one
    activation and a turn queue give — not one of those interleavings loses a thing. So what they pin is
    the absence of the lock, never a defect in the transitions themselves."""
    actor = _Actor()
    await actor.deliver(_delivery("first"))
    await actor.deliver(_delivery("second"))
    await actor.mark_seen({"notification_ids": ["first@FAIL"]})
    await actor.deliver(_delivery("third"))
    await _compaction_tick(actor)

    assert sorted(_stored_ids(actor)) == ["first@FAIL", "second@FAIL", "third@FAIL"]
    assert (_stored_meta(actor).unread, _stored_meta(actor).rows) == (2, 3)


# ── retention as an attack surface: whoever controls `eventTime` controls the cap ───────────────────


@pytest.mark.asyncio
async def test_a_flood_of_future_dated_pointers_evicts_every_genuine_unread_row_at_the_next_compaction() -> None:
    """The eviction case `test_adversarial_ingress.py` forward-references, driven to its end.

    `eventTime` is a producer string with no clock bound anywhere between the topic and the sort key,
    and compaction keeps the NEWEST unread rows up to the cap. Compose the two: rows stamped in the
    future sort above everything real, survive every trim, and push the recipient's genuine unread
    notifications out of their own inbox at the next tick. A failed run nobody is told about is exactly
    the outcome this plane exists to prevent, arriving through the mechanism that bounds storage.

    What it costs an attacker is stated rather than assumed: the author facet decides whose inbox this
    is, and under FGA the run's outputs must be ones the recipient may read — so the flood has to name a
    table the victim can see. With authorization off (the dev default) it needs neither.

    Rewrite when `occurred_at` is bounded against the receiver's own clock, or when compaction stops
    treating a producer's instant as a priority.
    """
    now = datetime.now(UTC)
    actor = _Actor()
    for index in range(5):
        await actor.deliver(_delivery(f"real-{index}", at=now - timedelta(minutes=index)))
    for index in range(5):
        await actor.deliver(_delivery(f"flood-{index}", at=now + timedelta(days=3650)))

    await _compaction_tick(actor)

    assert sorted(_stored_ids(actor)) == ["flood-0@FAIL", "flood-1@FAIL", "flood-2@FAIL", "flood-3@FAIL", "flood-4@FAIL"]


@pytest.mark.asyncio
async def test_a_pointer_dated_before_the_retention_window_is_trimmed_before_anyone_can_read_it() -> None:
    """The same lever pulled the other way, and it needs no volume at all: one run stamped far enough in
    the past is delivered, counted, and then removed by the first compaction tick that runs — so the
    recipient's badge goes back to zero without them ever opening the panel. The row is REAL and the
    trim is correct; what is missing is any lower bound on the instant it was trimmed against."""
    actor = _Actor()
    await actor.deliver(_delivery("ancient", at=datetime(1970, 1, 1, tzinfo=UTC)))
    assert _stored_meta(actor).unread == 1

    await _compaction_tick(actor)

    assert _stored_ids(actor) == []
    assert _stored_meta(actor).unread == 0


@pytest.mark.asyncio
async def test_nothing_bounds_an_inbox_between_compaction_ticks() -> None:
    """GAP, pinned. `inbox_max_rows` is applied by the compaction reminder and by nothing on the write
    path, so the cap is an eventual property with the reminder's own interval (6 h by default) as its
    resolution. Between two ticks an inbox grows to whatever the bus delivers, and every delivery
    rewrites the whole blob — so the cost is quadratic in the flood, not linear.

    Rewrite when `deliver` trims, or when the cap is documented as eventual on purpose.
    """
    actor = _Actor()
    cap = get_notifications_settings().inbox_max_rows

    for index in range(cap * 4):
        await actor.deliver(_delivery(f"run-{index}"))

    assert _stored_meta(actor).rows == cap * 4


@pytest.mark.asyncio
async def test_a_terminal_state_re_emitted_with_a_new_instant_never_moves_the_row_it_already_wrote() -> None:
    """A GUARANTEE, and the reason the two above are bounded rather than open-ended: dedupe is on
    `notification_id`, so a producer re-emitting one run's COMPLETE with a fresh `eventTime` cannot
    re-sort somebody's panel or resurrect a row they have already read. The FIRST instant is the one
    that sticks."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1", at=NOW))
    await actor.mark_seen({"notification_ids": ["run-1@FAIL"]})

    second = await actor.deliver(_delivery("run-1", at=NOW + timedelta(days=365)))
    stored = InboxRows.model_validate_json(actor.sm.store[ROWS_KEY]).pointers

    assert second["delivered"] is False
    assert [(pointer.occurred_at, pointer.seen) for pointer in stored] == [(NOW, True)]


# ── failure injection on the read plane ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/notifications/inbox", None),
        ("GET", "/notifications/inbox/unread", None),
        ("POST", "/notifications/inbox/seen", {"notification_ids": ["run-001@FAIL"]}),
        ("POST", "/notifications/inbox/dismiss", {"notification_id": "run-001@FAIL"}),
    ],
)
def test_a_state_store_outage_reaches_the_browser_as_a_bare_500(monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict[str, Any] | None) -> None:
    """GAP, pinned, and it undercuts a distinction this plane paid to make.

    `InboxUnreadable` exists so that "there is something here I refuse to serve" is a 503 problem+json
    rather than an empty 200 — the estate's own hard-won rule. The ORDINARY outage does not travel that
    path: an unreachable sidecar or state store raises whatever the SDK raises, `_translating` re-raises
    anything that is not an `InboxUnreadable` untouched, and no handler covers it. The caller gets a bare
    500 with no problem+json body, so a client cannot tell a broken inbox from a broken service, and the
    one refusal shape this plane guarantees is absent exactly when a dependency is down.

    Rewrite when the door maps a transport failure to 503-with-a-reason.
    """
    faulty = _Inbox(fault=RuntimeError("dapr: could not reach state store lance-statestore"))
    with TestClient(_door(faulty, monkeypatch), raise_server_exceptions=False) as door:
        response = door.request(method, path, json=body)

    assert response.status_code == 500
    assert not response.headers["content-type"].startswith("application/problem+json")


def test_the_refusal_body_carries_a_fixed_reason_while_the_envelope_goes_to_the_log(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A GUARANTEE, and it was a gap until the envelope was split off the wire.

    `_translating` used to carry the sidecar's message through VERBATIM, and `InboxUnreadable`'s message
    becomes the `detail` of a client-facing problem+json — so the body of a 503 was whatever daprd said,
    which is an INTERNAL envelope: the actor error code, the pod's own address, the state component's
    name, and anything else the runtime chose to embed. `fastapi`'s rule here is one line — internals
    never in the body — and this was the one place the plane broke it.

    Both halves are asserted, because either alone would be satisfied by a worse implementation: a body
    that says nothing AND a log that says nothing is not the fix, and neither is a body that says
    everything. The envelope below is synthetic and carries a credential to make the disclosure legible;
    what the test asserts is the mechanism, not that daprd ever emits a DSN.
    """
    envelope = "ERR_ACTOR_INVOKE_METHOD: InboxUnreadable at host=10.42.0.19:3500 store=lance-statestore dsn=postgres://lance:hunter2@pg:5432/lance"

    class _Sidecar:
        async def page(self, payload: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError(envelope)

    class _Proxy:
        def __getattr__(self, name: str) -> Any:
            return _translating(getattr(_Sidecar(), name))

    app = FastAPI()
    register_handlers(app)
    app.include_router(inbox_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.actors_registered = True
    app.state.fga = None
    app.dependency_overrides[security_module._deps.current_subject] = lambda: SUBJECT
    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, _Proxy()))

    with caplog.at_level(logging.ERROR, logger="notifications.proxies"), TestClient(app, raise_server_exceptions=False) as door:
        response = door.get("/notifications/inbox")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "hunter2" not in response.text
    assert response.json()["detail"] == "the inbox actor record cannot be read: the actor refused to serve this inbox record"
    assert any("hunter2" in str(getattr(record, "envelope", "")) for record in caplog.records), "the envelope has to survive somewhere an operator can read it"


def test_nothing_bounds_the_size_of_a_mark_seen_payload(client: TestClient, inbox: _Inbox) -> None:
    """GAP, pinned. `InboxMark.notification_ids` is a bare `list[str]` — no `max_length` on the list, no
    `max_length` on a member, and no body-size limit in `make_service_app`'s middleware. An authenticated
    caller therefore hands their own actor an arbitrarily large set, which the actor turns into a `set`
    and holds for the length of a turn during which nothing else about that inbox can proceed.

    Authenticated and self-targeted, so the blast radius is one person's own inbox — which is what makes
    it a bound rather than a defect, and why the assertion is on the ABSENCE of a limit rather than on
    any particular size.

    Rewrite when the mark payload declares a bound (`max_length` on the list is the whole fix).
    """
    inbox.rows = [_pointer(1)]

    response = client.post("/notifications/inbox/seen", json={"notification_ids": [f"run-{index}" for index in range(50_000)]})

    assert response.status_code == 200
    assert len(inbox.seen_payloads[-1]["notification_ids"]) == 50_000


# ── the claim-check invariant, on the surface nobody filters ───────────────────────────────────────


@pytest.mark.asyncio
async def test_the_drop_log_names_the_field_that_failed_and_never_what_it_held(caplog: pytest.LogCaptureFixture) -> None:
    """A GUARANTEE, and it was a gap until the DROP line stopped rendering the exception.

    `ingest_run_event` used to log `str(exc)`, and a pydantic message quotes the OFFENDING VALUE. The
    topic is ungoverned and multi-tenant, so what landed in this service's logs was a fragment of a
    payload belonging to whoever published it — never re-read through a governed path, and bounded in
    size by pydantic's own repr truncation and by nothing else. "No payload in the store" and "no
    payload anywhere" were different claims and only the first one was true.

    Both halves are asserted, because a line that says nothing would satisfy the first one alone: a DROP
    is the one outcome with no redelivery behind it to inspect later, so the line still has to name the
    field the producer got wrong. The metric labels are clean (`test_adversarial_ingress.py` pins that)
    and the stored pointer is clean; this is the third surface, which is why it keeps its own test.
    """
    hostile = {
        "eventType": "FAIL",
        "eventTime": {"note": "patient 4417", "token": "hunter2"},
        "run": {"runId": "run-1", "facets": {"author": {"sub": SUBJECT}}},
        "outputs": [{"name": "gold$restricted"}],
    }

    with caplog.at_level(logging.ERROR, logger="notifications.api.ingest"):
        status = await ingest_run_event(hostile, lane=Lane.BUS, visibility=Visibility(client=None, enabled=False), open_inbox=lambda _subject: cast(Any, None))

    logged = " ".join(f"{record.getMessage()} {getattr(record, 'faults', '')}" for record in caplog.records)
    assert status == DAPR_DROP
    assert "eventTime" in logged, "a DROP nobody can locate is a DROP nobody can fix"
    assert "patient 4417" not in logged
    assert "hunter2" not in logged


@pytest.mark.asyncio
async def test_a_stored_record_carries_no_field_a_reader_could_not_already_ask_for() -> None:
    """A GUARANTEE, asserted on the STORE rather than on the wire: the two partitions together hold an
    id, a reason, one object name, a run link, a lane sequence, two booleans and the bookkeeping the
    actor needs — and no field read off an event body. The wire shape is filtered by the response models;
    this is the layer under them, where nothing filters anything."""
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))

    meta = json.loads(actor.sm.store[META_KEY])
    rows = json.loads(actor.sm.store[ROWS_KEY])

    assert set(meta) == {"subject", "unread", "rows", "compaction_due_at", "updated_at"}
    assert set(rows) == {"subject", "pointers", "updated_at"}
    assert set(rows["pointers"][0]) == {"notification_id", "reason", "object_id", "source_run_id", "event_seq", "occurred_at", "seen", "dismissed"}


# ── a store that refuses itself, and the one door that answers anyway ───────────────────────────────

#: Every wire method, with a payload each accepts — so a refusal can be swept across the whole surface
#: rather than asserted on the one method that happens to be convenient.
_METHODS: list[tuple[str, dict[str, Any]]] = [
    ("page", {"limit": 10}),
    ("unread", {}),
    ("mark_seen", {"notification_ids": ["run-1@FAIL"]}),
    ("dismiss", {"notification_id": "run-1@FAIL"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "payload"), _METHODS)
async def test_an_unreadable_meta_record_refuses_every_call_rather_than_reading_as_an_empty_inbox(method: str, payload: dict[str, Any]) -> None:
    """A GUARANTEE, driven from the direction that would hurt. A record that has drifted out of its
    schema must refuse rather than read as absent: "you have nothing" is the answer that invites the
    caller's next write to overwrite what is really there, and the estate has paid for that twice.

    Swept across every method because one of them falling back to empty is enough to lose an inbox.
    """
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    actor.sm.store[META_KEY] = json.dumps({"subject": SUBJECT, "unread": "not-a-number"})

    with pytest.raises(InboxUnreadable):
        await getattr(actor, method)(**({} if method == "unread" else {"payload": payload}))


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "payload"), [entry for entry in _METHODS if entry[0] != "unread"])
async def test_an_unreadable_rows_record_refuses_every_call_that_reads_rows(method: str, payload: dict[str, Any]) -> None:
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    actor.sm.store[ROWS_KEY] = json.dumps({"subject": SUBJECT, "pointers": "not-a-list", "updated_at": NOW.isoformat()})

    with pytest.raises(InboxUnreadable):
        await getattr(actor, method)(payload)


@pytest.mark.asyncio
async def test_the_badge_still_answers_over_a_row_set_it_could_not_read() -> None:
    """GAP, pinned, and the exception the sweep above has to name rather than hide.

    `unread` reads the small partition alone — which is the entire reason the state is split, and by far
    the most frequent read in the plane. So when the ROWS record is unreadable the badge keeps answering
    from a count derived from rows nobody can load: the panel 503s and the bell beside it says three.

    Defensible (the number is still the last true count, and failing the badge would take the whole shell
    down over one bad record) and undeclared, which is the part worth a test.

    Rewrite when the meta carries a marker that its rows are unreadable, or when the badge refuses too.
    """
    actor = _Actor()
    await actor.deliver(_delivery("run-1"))
    actor.sm.store[ROWS_KEY] = json.dumps({"subject": SUBJECT, "pointers": "not-a-list", "updated_at": NOW.isoformat()})

    assert await actor.unread() == {"unread": 1, "rows": 1}
    with pytest.raises(InboxUnreadable):
        await actor.page({"limit": 10})


# ── no sidecar at all ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_opening_an_inbox_blocks_the_whole_event_loop_while_it_looks_for_a_sidecar() -> None:
    """GAP, pinned — a defect of the SDK's shape that this service inherits per request.

    `typed_proxy` calls `ActorProxy.create` on every call, and the SDK's HTTP client runs
    `DaprHealth.wait_for_sidecar()` in its constructor: a SYNCHRONOUS `urllib` poll with `time.sleep`
    between attempts, `DAPR_HEALTH_TIMEOUT` (60 s by default) long. Called from an `async def` route it
    blocks the event loop — so with the sidecar gone, one inbox request stalls every other request in the
    process for a minute, and with it present each request still pays a blocking localhost round-trip.

    `require_actor_plane` does not cover this: it reads a LOCAL registration flag, which its own docstring
    says must not be read as "the sidecar is there". Registration succeeds at startup and the sidecar dies
    afterwards is exactly the case it cannot see.

    The port is moved to a closed one and the timeout shortened so the test states the mechanism in a
    fraction of a second instead of a minute.

    Rewrite when the proxy is built once in the lifespan, or the health check is off the request path.
    """
    from dapr.conf import settings as dapr_settings

    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.005)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dapr_settings, "DAPR_HEALTH_TIMEOUT", 0.25)
        patch.setattr(dapr_settings, "DAPR_HTTP_PORT", 9)  # discard — nothing listens, so the poll cannot succeed
        ticker = asyncio.create_task(_tick())
        await asyncio.sleep(0.02)
        ticked_before = ticks
        started = time.monotonic()

        with pytest.raises(TimeoutError):
            inbox_for(SUBJECT)

        elapsed = time.monotonic() - started
        ticker.cancel()

    assert elapsed >= 0.25
    assert ticks == ticked_before, "the event loop kept running, so the health check is not blocking after all"
