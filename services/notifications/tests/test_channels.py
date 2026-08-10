"""Channels — the push that leaves the estate, and the ledger that keeps it from leaving twice."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import prefs as prefs_module
from notifications.api.channels import EMAIL, SLACK, deliver_to_channels, render
from notifications.config import get_notifications_settings
from notifications.models import InboxPointer, NotificationReason


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
