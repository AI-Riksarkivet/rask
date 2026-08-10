"""Channels — the push that leaves the estate, and the ledger that keeps it from leaving twice."""

from datetime import UTC, datetime
from typing import Any, cast, override

import pytest
from dapr.actor import ActorId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import prefs as prefs_module
from notifications.api.channels import EMAIL, SLACK, deliver_to_channels, render
from notifications.api.fanout import InboxOpener
from notifications.config import get_notifications_settings
from notifications.inbox_actor import InboxActor
from notifications.models import InboxPointer, NotificationReason
from notifications.proxies import inbox_actor_id


def _pointer(*, notification_id: str = "run-1@FAIL", sent: list[str] | None = None) -> InboxPointer:
    return InboxPointer(
        notification_id=notification_id,
        reason=NotificationReason.AUTHOR,
        object_id="silver$pages",
        source_run_id="run-1",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        sent=sent or [],
    )


class _Ledger:
    """The actor's claim, in memory. `claim` is check-and-write, as the real one is inside its turn."""

    def __init__(self, already: set[tuple[str, str]] | None = None) -> None:
        self.claimed = set(already or ())

    async def claim(self, notification_id: str, channel: str) -> bool:
        key = (notification_id, channel)
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class _Recorder:
    def __init__(self, *, fails: bool = False) -> None:
        self.sends: list[dict[str, str]] = []
        self.fails = fails

    async def __call__(self, *, destination: str, subject_line: str, body: str) -> None:
        if self.fails:
            raise RuntimeError("the provider is down")
        self.sends.append({"destination": destination, "subject": subject_line, "body": body})


class TestRender:
    def test_the_message_is_built_from_the_pointer_alone(self) -> None:
        """The claim-check invariant crosses the channel boundary: an email is exactly as disclosing
        as the inbox row it announces, which is what makes it safe to send to an ungoverned address."""
        headline, body = render(_pointer())
        assert "silver$pages" in headline
        assert "run-1" in body
        assert "Fail" in headline

    def test_it_carries_no_event_payload(self) -> None:
        _, body = render(_pointer())
        # Nothing read off an event body — no error text, no facets, no outputs beyond the one object.
        assert len(body.splitlines()) <= 5


class TestDeliverToChannels:
    @pytest.mark.asyncio
    async def test_an_opted_in_channel_is_sent_once(self) -> None:
        email, ledger = _Recorder(), _Ledger()

        sent = await deliver_to_channels(_pointer(), channels=[EMAIL], destinations={EMAIL: "a@b.c"}, table={EMAIL: email}, mark_sent=ledger.claim)

        assert sent == [EMAIL]
        assert email.sends[0]["destination"] == "a@b.c"

    @pytest.mark.asyncio
    async def test_a_redelivery_sends_nothing(self) -> None:
        """JetStream is at-least-once. Unlike a duplicated inbox row, which the actor collapses
        silently, a duplicated email is a thing a person sees."""
        email = _Recorder()
        ledger = _Ledger(already={("run-1@FAIL", EMAIL)})

        sent = await deliver_to_channels(_pointer(), channels=[EMAIL], destinations={EMAIL: "a@b.c"}, table={EMAIL: email}, mark_sent=ledger.claim)

        assert sent == []
        assert email.sends == []

    @pytest.mark.asyncio
    async def test_the_claim_happens_BEFORE_the_send(self) -> None:
        """Recording afterwards leaves the window that matters: a crash between the send and the write
        re-sends on redelivery."""
        ledger = _Ledger()
        order: list[str] = []

        async def claim(nid: str, channel: str) -> bool:
            order.append("claim")
            return await ledger.claim(nid, channel)

        async def send(**_: Any) -> None:
            order.append("send")

        await deliver_to_channels(_pointer(), channels=[EMAIL], destinations={EMAIL: "a@b.c"}, table={EMAIL: send}, mark_sent=claim)

        assert order == ["claim", "send"]

    @pytest.mark.asyncio
    async def test_one_channels_failure_never_stops_another(self) -> None:
        """Batch with partial failure: independent destinations, and a Slack outage must not stop the
        email."""
        email, slack, ledger = _Recorder(), _Recorder(fails=True), _Ledger()

        sent = await deliver_to_channels(
            _pointer(),
            channels=[SLACK, EMAIL],
            destinations={SLACK: "https://hook", EMAIL: "a@b.c"},
            table={SLACK: slack, EMAIL: email},
            mark_sent=ledger.claim,
        )

        assert sent == [EMAIL]
        assert len(email.sends) == 1

    @pytest.mark.asyncio
    async def test_a_failed_send_is_not_retried_by_re_claiming(self) -> None:
        """The claim STANDS after a failure, deliberately: under at-least-once, re-sending is the
        failure a person SEES, while a missed push is one the bell already covers."""
        slack, ledger = _Recorder(fails=True), _Ledger()

        await deliver_to_channels(_pointer(), channels=[SLACK], destinations={SLACK: "https://hook"}, table={SLACK: slack}, mark_sent=ledger.claim)

        assert ("run-1@FAIL", SLACK) in ledger.claimed

    @pytest.mark.asyncio
    async def test_a_channel_with_no_destination_is_skipped_quietly(self) -> None:
        """A preference must survive a destination being cleared — skipping is honest, raising is not."""
        email, ledger = _Recorder(), _Ledger()

        sent = await deliver_to_channels(_pointer(), channels=[EMAIL], destinations={}, table={EMAIL: email}, mark_sent=ledger.claim)

        assert sent == []
        assert ledger.claimed == set(), "an unsendable channel must not consume its claim"

    @pytest.mark.asyncio
    async def test_a_channel_this_build_does_not_ship_is_skipped(self) -> None:
        """Removing a channel from the build must not make a stored preference unreadable."""
        ledger = _Ledger()

        sent = await deliver_to_channels(_pointer(), channels=["carrier-pigeon"], destinations={"carrier-pigeon": "coop"}, table={}, mark_sent=ledger.claim)

        assert sent == []

    @pytest.mark.asyncio
    async def test_no_opt_in_means_nothing_leaves_the_estate(self) -> None:
        """OFF by default is the whole posture: the bell costs a reader nothing, an email does not."""
        email, ledger = _Recorder(), _Ledger()

        sent = await deliver_to_channels(_pointer(), channels=[], destinations={EMAIL: "a@b.c"}, table={EMAIL: email}, mark_sent=ledger.claim)

        assert sent == []
        assert email.sends == []


class _FakeInbox:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    async def get_prefs(self) -> dict[str, Any]:
        return {"channels": list(self._store.get("channels", [])), "destinations": dict(self._store.get("destinations", {}))}

    async def set_prefs(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._store["channels"] = list(payload.get("channels") or [])
        self._store["destinations"] = dict(payload.get("destinations") or {})
        return await self.get_prefs()


@pytest.fixture
def prefs_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, Any]]:
    store: dict[str, Any] = {}
    monkeypatch.setattr(prefs_module, "inbox_for", lambda _subject: _FakeInbox(store))
    app = FastAPI()
    app.include_router(prefs_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.actors_registered = True
    return TestClient(app, raise_server_exceptions=False), store


class TestPrefsDoor:
    def test_absent_prefs_read_as_off(self, prefs_client: tuple[TestClient, dict[str, Any]]) -> None:
        client, _ = prefs_client
        assert client.get("/prefs").json() == {"channels": [], "destinations": {}}

    def test_an_opt_in_round_trips(self, prefs_client: tuple[TestClient, dict[str, Any]]) -> None:
        client, _ = prefs_client

        client.put("/prefs", json={"channels": [EMAIL], "destinations": {EMAIL: "a@b.c"}})

        assert client.get("/prefs").json() == {"channels": [EMAIL], "destinations": {EMAIL: "a@b.c"}}

    def test_a_channel_with_no_destination_is_refused_rather_than_stored(self, prefs_client: tuple[TestClient, dict[str, Any]]) -> None:
        """A channel that is on and unreachable is silently identical to one that is off — and the
        subject would have every reason to believe they had turned it on."""
        client, store = prefs_client

        response = client.put("/prefs", json={"channels": [EMAIL], "destinations": {}})

        # 400, the estate's `ValidationError` — a well-formed body making an unsatisfiable request.
        assert response.status_code == 400
        assert EMAIL in response.text
        assert store == {}

    def test_an_unknown_channel_is_refused(self, prefs_client: tuple[TestClient, dict[str, Any]]) -> None:
        client, _ = prefs_client

        response = client.put("/prefs", json={"channels": ["carrier-pigeon"], "destinations": {"carrier-pigeon": "coop"}})

        assert response.status_code == 400

    def test_prefs_are_replaced_wholesale_not_merged(self, prefs_client: tuple[TestClient, dict[str, Any]]) -> None:
        """A partial merge makes "I removed my Slack address" indistinguishable from "I did not
        mention it"."""
        client, _ = prefs_client
        client.put("/prefs", json={"channels": [EMAIL, SLACK], "destinations": {EMAIL: "a@b.c", SLACK: "https://hook"}})

        client.put("/prefs", json={"channels": [EMAIL], "destinations": {EMAIL: "a@b.c"}})

        assert client.get("/prefs").json()["channels"] == [EMAIL]


# --- the fan-out hook: WHEN a channel push happens, which is the whole safety property ------------


class _Pushes:
    def __init__(self, *, fails: bool = False) -> None:
        self.calls: list[str] = []
        self.fails = fails

    async def __call__(self, subject: str, payload: dict[str, Any]) -> None:
        self.calls.append(subject)
        if self.fails:
            raise RuntimeError("the binding is down")


class _InboxStub:
    """Returns whichever delivery verdict a case needs, so the hook's ordering can be driven."""

    def __init__(self, delivered: bool = True, *, raises: bool = False) -> None:
        self.delivered = delivered
        self.raises = raises

    async def deliver(self, _payload: dict[str, Any]) -> dict[str, Any]:
        if self.raises:
            raise RuntimeError("the sidecar is unreachable")
        return {"delivered": self.delivered}


async def _run_fanout(*, delivered: bool = True, raises: bool = False, visible: bool = True, pushes: _Pushes) -> None:
    from notifications.api.fanout import fan_out
    from notifications.api.lineage_events import Notifiable
    from notifications.api.visibility import Visibility

    notice = Notifiable(
        delivery=_pointer(),
        author="alice",
        outputs=frozenset({"silver$pages"}) if visible else frozenset({"forbidden$table"}),
    )

    class _View(Visibility):
        # Parameter NAMES match the base, not just the types: they are keyword-callable, so renaming
        # them is a real override violation rather than a checker technicality.
        async def sees_all(self, subject: str, names: Any) -> bool:
            return visible

    await fan_out(
        notice,
        audience=["alice"],
        visibility=_View(client=None, enabled=False),
        open_inbox=cast(InboxOpener, lambda _s: _InboxStub(delivered, raises=raises)),
        push=pushes,
    )


class TestFanoutHook:
    @pytest.mark.asyncio
    async def test_a_written_row_pushes(self) -> None:
        pushes = _Pushes()
        await _run_fanout(pushes=pushes)
        assert pushes.calls == ["alice"]

    @pytest.mark.asyncio
    async def test_a_DUPLICATE_row_pushes_nothing(self) -> None:
        """The row already existed, so a push here is how a redelivery becomes a second email."""
        pushes = _Pushes()
        await _run_fanout(delivered=False, pushes=pushes)
        assert pushes.calls == []

    @pytest.mark.asyncio
    async def test_a_HIDDEN_row_pushes_nothing(self) -> None:
        """Pushing for a row the visibility gate just refused would tell someone about a run by
        email that the inbox deliberately withheld — the leak the gate exists to prevent, taking the
        one route that leaves the estate."""
        pushes = _Pushes()
        await _run_fanout(visible=False, pushes=pushes)
        assert pushes.calls == []

    @pytest.mark.asyncio
    async def test_a_FAILED_write_pushes_nothing(self) -> None:
        pushes = _Pushes()
        await _run_fanout(raises=True, pushes=pushes)
        assert pushes.calls == []

    @pytest.mark.asyncio
    async def test_a_push_failure_does_not_change_the_outcome(self) -> None:
        """The inbox row is written and the bell is correct whatever the channel plane does. Flipping
        to RETRY would redeliver the whole event to re-attempt an email — re-writing nothing, to retry
        the one thing a person would see twice."""
        from notifications.api.fanout import fan_out
        from notifications.api.lineage_events import Notifiable
        from notifications.api.visibility import Visibility

        notice = Notifiable(delivery=_pointer(), author="alice", outputs=frozenset({"silver$pages"}))
        result = await fan_out(
            notice,
            audience=["alice"],
            visibility=Visibility(client=None, enabled=False),
            open_inbox=cast(InboxOpener, lambda _s: _InboxStub(True)),
            push=_Pushes(fails=True),
        )

        assert result.delivered == 1
        assert not result.needs_retry


# --- idempotency against the REAL actor claim, not a fake ledger ----------------------------------
#
# Every test above stubs `mark_sent`. That proves the dispatch loop honours a claim; it cannot prove
# the CLAIM ITSELF, which is the part that has to hold under redelivery. These drive the real
# `InboxActor.claim_channel` — check-and-write inside the turn — so what is under test is the actual
# state transition rather than a test double agreeing with itself.
#
# What is still NOT proven here, and is named rather than implied: a real broker redelivering to a
# real sidecar against a real SMTP conversation. That is `make notifications-rig-up` (Mailpit + a
# COUNTING Slack sink), because "exactly one message left the estate" is a claim about the thing a
# person sees, and no in-process test can make it.


class _ActorSm:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def set_state_ttl(self, key: str, value: str, _ttl: Any) -> None:
        self.store[key] = value

    async def save_state(self) -> None:
        return None


def _delivery(notification_id: str = "run-1@FAIL") -> dict[str, Any]:
    """A DELIVERY payload — no `seen`/`dismissed`/`sent`. Those are the subject's, minted by the actor,
    and `NotificationDelivery` forbids them precisely so a forged delivery cannot arrive pre-read."""
    return {
        "notification_id": notification_id,
        "reason": "author",
        "object_id": "silver$pages",
        "source_run_id": notification_id.split("@")[0],
        "occurred_at": "2026-08-10T12:00:00+00:00",
    }


class _RealActor(InboxActor):
    """The real actor with its Dapr plumbing replaced — the `test_inbox_actor` shape.

    A SUBCLASS rather than a monkey-patched instance: `register_reminder` is a bound method with a
    declared signature, and assigning over it makes the double disagree with the base in a way the
    type checker is right to reject. Overriding keeps the contract.
    """

    def __init__(self, subject: str = "alice") -> None:
        self._state_manager = cast(Any, _ActorSm())
        self.id = ActorId(inbox_actor_id(subject))
        self.registered: list[str] = []

    @override
    async def register_reminder(self, name: str, state: bytes, due_time: Any, period: Any = None, ttl: Any = None, failure_policy: Any = None) -> None:
        self.registered.append(name)

    @override
    async def unregister_reminder(self, name: str) -> None:
        return None


def _real_actor(subject: str = "alice") -> _RealActor:
    return _RealActor(subject)


class TestRealClaimUnderRedelivery:
    @pytest.mark.asyncio
    async def test_the_same_event_delivered_twice_claims_once(self) -> None:
        """THE property, against the real state transition: at-least-once delivery, exactly-once send."""
        actor = _real_actor()
        await actor.deliver(_delivery())

        first = await actor.claim_channel({"notification_id": "run-1@FAIL", "channel": EMAIL})
        second = await actor.claim_channel({"notification_id": "run-1@FAIL", "channel": EMAIL})

        assert first["claimed"] is True
        assert second["claimed"] is False

    @pytest.mark.asyncio
    async def test_two_channels_claim_independently(self) -> None:
        """The key is `(event, subject, CHANNEL)` — an email must not suppress the Slack message."""
        actor = _real_actor()
        await actor.deliver(_delivery())

        assert (await actor.claim_channel({"notification_id": "run-1@FAIL", "channel": EMAIL}))["claimed"] is True
        assert (await actor.claim_channel({"notification_id": "run-1@FAIL", "channel": SLACK}))["claimed"] is True

    @pytest.mark.asyncio
    async def test_an_unknown_notification_claims_nothing(self) -> None:
        """Inventing a row to hold a ledger entry would resurrect a notification that was compacted
        or dismissed — so a claim against a row that is gone fails rather than creating one."""
        actor = _real_actor()

        assert (await actor.claim_channel({"notification_id": "ghost@FAIL", "channel": EMAIL}))["claimed"] is False

    @pytest.mark.asyncio
    async def test_the_claim_SURVIVES_a_full_round_trip_through_state(self) -> None:
        """The ledger rides the pointer, so it has to survive being serialized and read back — which
        is the whole reason it is a stored field rather than an in-memory set."""
        actor = _real_actor()
        await actor.deliver(_delivery())
        await actor.claim_channel({"notification_id": "run-1@FAIL", "channel": EMAIL})

        page = await actor.page({"limit": 10})

        assert page["pointers"][0]["sent"] == [EMAIL]


class TestDigest:
    @pytest.mark.asyncio
    async def test_a_failure_is_never_digested(self) -> None:
        """Batching the one notification someone needs NOW, to reduce the volume of the ones they did
        not mind, is the trade nobody wants made for them — and the one a naive digest makes."""
        from notifications.models import ChannelPrefs

        prefs = ChannelPrefs(subject="alice", digest_seconds=3600, updated_at=datetime.now(UTC))

        assert prefs.digest_defers("run-1@FAIL") is False
        assert prefs.digest_defers("run-1@ABORT") is False
        assert prefs.digest_defers("run-1@COMPLETE") is True

    @pytest.mark.asyncio
    async def test_with_no_digest_nothing_defers(self) -> None:
        from notifications.models import ChannelPrefs

        prefs = ChannelPrefs(subject="alice", updated_at=datetime.now(UTC))

        assert prefs.digest_defers("run-1@COMPLETE") is False

    @pytest.mark.asyncio
    async def test_arming_twice_does_not_push_the_window_forward(self) -> None:
        """A steady trickle of deferred notifications must not keep resetting the window — a subject
        receiving one every few seconds would otherwise never see a digest at all."""
        actor = _real_actor()

        first = await actor.arm_digest({"seconds": 3600})
        second = await actor.arm_digest({"seconds": 3600})

        assert first["armed"] is True
        assert second["armed"] is False
        assert actor.registered.count("digest") == 1

    @pytest.mark.asyncio
    async def test_draining_returns_the_unread_unsent_rows_and_closes_the_window(self) -> None:
        """Rows already pushed are excluded: a failure went out immediately, and repeating it in the
        digest an hour later is the duplicate a person notices most."""
        actor = _real_actor()
        await actor.deliver(_delivery("run-1@COMPLETE"))
        await actor.deliver(_delivery("run-2@FAIL"))
        await actor.claim_channel({"notification_id": "run-2@FAIL", "channel": EMAIL})
        await actor.arm_digest({"seconds": 3600})

        drained = await actor.drain_digest()

        assert [p["notification_id"] for p in drained["pointers"]] == ["run-1@COMPLETE"]
        assert (await actor.arm_digest({"seconds": 3600}))["armed"] is True, "the window did not close"
