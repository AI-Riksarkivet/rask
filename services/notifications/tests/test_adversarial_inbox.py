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
import json
import logging
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
from notifications.inbox_actor import META_KEY, ROWS_KEY, InboxActor
from notifications.models import InboxDismiss, InboxMark, InboxPointer, InboxQuery, InboxRows, NotificationReason, notification_id
from notifications.proxies import TypedActorProxy, _translating, inbox_actor_id
from service_kit.exceptions import register_handlers


SUBJECT = "alice"
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


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


# ── the door, driven against an inbox double ────────────────────────────────────────────────────────


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


# ── who an actor id names: the second lock is not total ─────────────────────────────────────────────


@pytest.mark.parametrize("subject", [""])
def test_a_verified_token_with_an_unusable_subject_crashes_the_door_instead_of_refusing_it(subject: str) -> None:
    """HALF-CLOSED, and the remaining half is still pinned. `IDToken.sub` is a bare `str` with no
    `min_length`, so a conformant-looking token can carry an empty (or whitespace-only) one, and
    `current_subject` hands it straight on.

    `inbox_actor_id` then does the right thing — there is no anonymous inbox, so it raises — but nothing
    on this path raises a `DomainError`, so the answer depended entirely on whether a catch-all existed.
    It did not: `register_handlers` mapped `DomainError` and `RequestValidationError` and nothing else,
    so the caller got a BARE 500 with no problem+json body — the one shape this plane's refusals are
    supposed never to take. That half is fixed (open_fastapi-audit, the missing-catch-all finding):
    `register_handlers` now installs an `Exception` handler, so the envelope is problem+json and the
    traceback goes to the log instead of nowhere.

    WHAT IS STILL WRONG, and why this test keeps its name: 500 is not the honest status. An unusable
    subject is a bad credential, not a server fault, and the door should refuse it BY NAME. Until it
    does, monitoring reads a rejected token as this service crashing.

    The REAL `inbox_for` has to run for this, so the actor plane is deliberately not patched out — the
    refusal happens while composing the id, before any sidecar channel is opened.

    Rewrite again when the door refuses an unusable subject by name.
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

    # The envelope is now guaranteed; the STATUS is the half still open.
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.status_code == 500


# ── retention as an attack surface: whoever controls `eventTime` controls the cap ───────────────────


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
    ],
)
def test_a_state_store_outage_reaches_the_browser_as_a_bare_500(monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict[str, Any] | None) -> None:
    """HALF-CLOSED, and the remaining half is still pinned.

    `InboxUnreadable` exists so that "there is something here I refuse to serve" is a 503 problem+json
    rather than an empty 200 — the estate's own hard-won rule. The ORDINARY outage does not travel that
    path: an unreachable sidecar or state store raises whatever the SDK raises, and `_translating`
    re-raises anything that is not an `InboxUnreadable` untouched. With no catch-all, that reached the
    browser as a bare 500 with no problem+json body at all.

    The ENVELOPE half is fixed (open_fastapi-audit, the missing-catch-all finding): `register_handlers`
    now installs an `Exception` handler, so every refusal on this plane is problem+json and the SDK's
    message goes to the log rather than the wire.

    WHAT IS STILL WRONG: the STATUS. A dependency being down is a 503, and this answers 500, so a client
    still cannot tell a broken state store from a broken service — the distinction `InboxUnreadable` was
    built to make, absent exactly when a dependency is down.

    Rewrite again when the door maps a transport failure to 503-with-a-reason.
    """
    faulty = _Inbox(fault=RuntimeError("dapr: could not reach state store lance-statestore"))
    with TestClient(_door(faulty, monkeypatch), raise_server_exceptions=False) as door:
        response = door.request(method, path, json=body)

    # The envelope is now guaranteed; the STATUS is the half still open.
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.status_code == 500


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
