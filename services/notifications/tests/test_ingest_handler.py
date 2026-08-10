"""The ingress matrix: DROP, RETRY, SUCCESS — and the audience rules behind each answer.

The statuses are not three flavours of "handled". DROP is the only answer to a payload redelivery
cannot fix, RETRY is the only answer to a dependency that was briefly unreachable, and getting them
the wrong way round is how a subscription stops delivering anything at all. Each case below names
which of the two it is holding in place.
"""

from typing import TYPE_CHECKING, Any, cast

import pytest

from notifications.api import visibility as visibility_module
from notifications.api.fanout import fan_out
from notifications.api.ingest import DAPR_DROP, DAPR_RETRY, DAPR_SUCCESS, ingest_run_event
from notifications.api.lineage_events import LineageRunEvent, notifiable
from notifications.api.metrics import Lane
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


OPEN = Visibility(client=None, enabled=False)
#: A wired client. Only its identity matters — `_Checks` below is what answers.
WIRED = cast("OpenFgaClient", object())


def _event(
    *,
    event_type: str = "FAIL",
    run_id: str = "run-1",
    author: str | None = "alice",
    outputs: list[str] | None = None,
) -> dict[str, Any]:
    facets: dict[str, Any] = {} if author is None else {"author": {"name": author, "sub": author}}
    return {
        "eventType": event_type,
        "eventTime": "2026-08-09T12:00:00+00:00",
        "run": {"runId": run_id, "facets": facets},
        "outputs": [{"namespace": "silver", "name": name} for name in (outputs if outputs is not None else ["silver$pages"])],
    }


class _Inbox:
    """One subject's inbox, with the REAL actor's idempotency contract: a repeated
    `notification_id` is not delivered twice and says so."""

    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._subject in self._plane.broken:
            raise RuntimeError("the sidecar is unreachable")
        rows = self._plane.boxes.setdefault(self._subject, [])
        if any(row["notification_id"] == payload["notification_id"] for row in rows):
            return {"delivered": False, "unread": len(rows), "rows": len(rows)}
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _Plane:
    """A whole actor plane in a dict, addressable the way `inbox_for` addresses the real one."""

    def __init__(self, broken: set[str] | None = None) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}
        self.broken = broken or set()

    def open(self, subject: str) -> TypedActorProxy:
        # `cast` at the injection point only: the double implements the one method the fan-out calls,
        # and narrowing it any further would mean inheriting Dapr's proxy to run a unit test.
        return cast(TypedActorProxy, _Inbox(self, subject))


class _Checks:
    """Stands in for `fga.batch_check`."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    async def __call__(self, client: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        return {obj: obj in self.allowed for obj in objects}


def _governed(monkeypatch: pytest.MonkeyPatch, allowed: set[str]) -> Visibility:
    monkeypatch.setattr(visibility_module.fga, "batch_check", _Checks(allowed))
    return Visibility(client=WIRED, enabled=True)


# --- DROP: permanent, and never RETRY ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, "a string", 7, {}, {"eventType": "FAIL"}, {"eventType": "FAIL", "eventTime": "nope", "run": {"runId": "r"}}])
async def test_an_unparseable_payload_is_dropped_never_retried(payload: object) -> None:
    """Redelivery cannot make a malformed payload parse. RETRYing it returns the same message
    forever and everything queued behind it waits — the poisoned subscription."""
    plane = _Plane()
    assert await ingest_run_event(payload, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_DROP
    assert plane.boxes == {}


# --- SUCCESS with nobody told: handled, so there is nothing to redeliver -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["START", "RUNNING"])
async def test_a_non_terminal_state_is_acked_and_told_to_nobody(event_type: str) -> None:
    plane = _Plane()
    assert await ingest_run_event(_event(event_type=event_type), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_a_run_with_no_verified_author_is_acked_and_told_to_nobody() -> None:
    plane = _Plane()
    assert await ingest_run_event(_event(author=None), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_a_dataset_less_run_is_acked_and_told_to_nobody() -> None:
    """The governed read path drops these when FGA is on because they pass the visibility test
    vacuously; this plane refuses them with FGA on AND off, because a pointer has to name an object."""
    plane = _Plane()
    assert await ingest_run_event(_event(outputs=[]), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert plane.boxes == {}


# --- SUCCESS with delivery ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_terminal_run_lands_one_pointer_in_its_authors_inbox() -> None:
    plane = _Plane()
    assert await ingest_run_event(_event(), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert list(plane.boxes) == ["alice"]
    assert plane.boxes["alice"][0]["notification_id"] == "run-1@FAIL"


@pytest.mark.asyncio
async def test_nobody_but_the_author_is_told() -> None:
    """v1's audience IS the author. Membership gates watching and never implies it — the failure this
    plane exists to end is a badge that counts other people's work."""
    plane = _Plane()
    await ingest_run_event(_event(author="alice"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    await ingest_run_event(_event(run_id="run-2", author="bob"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    assert sorted(plane.boxes) == ["alice", "bob"]
    assert len(plane.boxes["alice"]) == 1
    assert len(plane.boxes["bob"]) == 1


@pytest.mark.asyncio
async def test_an_at_least_once_redelivery_lands_one_pointer() -> None:
    """JetStream delivery is at-least-once and there is no window that survives a pod restart, so the
    natural key has to be the guard."""
    plane = _Plane()
    for _ in range(3):
        assert await ingest_run_event(_event(), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_SUCCESS
    assert len(plane.boxes["alice"]) == 1


@pytest.mark.asyncio
async def test_a_retried_run_re_emitting_its_terminal_state_lands_one_pointer() -> None:
    """The three-column key `(run_id, event_type, event_time)` cannot dedupe this: a
    RETRY-after-partial-success re-emits the same run's terminal event with a FRESH eventTime, which
    is exactly why lineage carries a second partial index on `(run_id, event_type)` — and why the
    notification id encodes that pair rather than the instant."""
    plane = _Plane()
    first = _event()
    again = _event()
    again["eventTime"] = "2026-08-09T18:45:00+00:00"

    await ingest_run_event(first, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    await ingest_run_event(again, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)

    assert len(plane.boxes["alice"]) == 1


@pytest.mark.asyncio
async def test_the_same_run_in_two_states_lands_two_pointers() -> None:
    """`run_id@STATE`, so dismissing a run's earlier state leaves its later one free to arrive."""
    plane = _Plane()
    await ingest_run_event(_event(event_type="COMPLETE"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    await ingest_run_event(_event(event_type="FAIL"), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)
    assert sorted(row["notification_id"] for row in plane.boxes["alice"]) == ["run-1@COMPLETE", "run-1@FAIL"]


# --- the visibility re-check, on the ungoverned lane -------------------------------------------


@pytest.mark.asyncio
async def test_an_author_who_cannot_see_the_output_is_not_told(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bus is UNGOVERNED — a subscriber sees every tenant's runs — so a pointer written straight
    off a delivery would be an authorization decision made by whoever published the event."""
    plane = _Plane()
    view = _governed(monkeypatch, allowed=set())
    assert await ingest_run_event(_event(), lane=Lane.BUS, visibility=view, open_inbox=plane.open) == DAPR_SUCCESS
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_one_invisible_output_out_of_two_drops_the_whole_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    plane = _Plane()
    view = _governed(monkeypatch, allowed={"table:silver$pages"})
    await ingest_run_event(_event(outputs=["silver$pages", "gold$lines"]), lane=Lane.BUS, visibility=view, open_inbox=plane.open)
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_a_fully_visible_run_is_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    plane = _Plane()
    view = _governed(monkeypatch, allowed={"table:silver$pages", "table:gold$lines"})
    await ingest_run_event(_event(outputs=["silver$pages", "gold$lines"]), lane=Lane.BUS, visibility=view, open_inbox=plane.open)
    assert len(plane.boxes["alice"]) == 1


@pytest.mark.asyncio
async def test_authorization_enabled_but_unwired_retries_rather_than_delivering() -> None:
    """Fail-closed, on the ingest lane too: a broken authorization layer must not become an open one,
    and RETRY is the answer that neither delivers nor discards while it is broken."""
    plane = _Plane()
    unwired = Visibility(client=None, enabled=True)
    assert await ingest_run_event(_event(), lane=Lane.BUS, visibility=unwired, open_inbox=plane.open) == DAPR_RETRY
    assert plane.boxes == {}


# --- RETRY, and partial failure ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreachable_actor_plane_retries() -> None:
    plane = _Plane(broken={"alice"})
    assert await ingest_run_event(_event(), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open) == DAPR_RETRY


@pytest.mark.asyncio
async def test_one_bad_recipient_never_aborts_the_audience() -> None:
    """A fan-out that stops at the first failure delivers to some subset of the people who should
    have had it, and the subset depends on iteration order."""
    plane = _Plane(broken={"bob"})
    notice = notifiable(LineageRunEvent.model_validate(_event()))
    assert notice is not None

    result = await fan_out(notice, audience=["bob", "alice", "carol"], visibility=OPEN, open_inbox=plane.open)

    assert (result.delivered, result.failed) == (2, 1)
    assert sorted(plane.boxes) == ["alice", "carol"]


@pytest.mark.asyncio
async def test_a_partial_failure_asks_for_a_retry_and_the_retry_is_a_no_op_for_the_rest() -> None:
    """RETRY re-drives the WHOLE audience, which is only safe because delivery is idempotent — so the
    recipients who already have the pointer count as duplicates rather than being told twice."""
    plane = _Plane(broken={"bob"})
    notice = notifiable(LineageRunEvent.model_validate(_event()))
    assert notice is not None

    first = await fan_out(notice, audience=["bob", "alice"], visibility=OPEN, open_inbox=plane.open)
    assert first.needs_retry

    plane.broken.clear()
    second = await fan_out(notice, audience=["bob", "alice"], visibility=OPEN, open_inbox=plane.open)

    assert (second.delivered, second.duplicate, second.failed) == (1, 1, 0)
    assert len(plane.boxes["alice"]) == 1
    assert len(plane.boxes["bob"]) == 1


@pytest.mark.asyncio
async def test_a_hidden_recipient_is_counted_apart_from_a_failed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hidden and failed must not share a counter. Not seeing a run is the system working; a store
    that could not be reached is an outage, and an alert that cannot tell them apart fires on the
    ordinary case or never fires at all."""
    plane = _Plane()
    view = _governed(monkeypatch, allowed=set())
    notice = notifiable(LineageRunEvent.model_validate(_event()))
    assert notice is not None

    result = await fan_out(notice, audience=["alice"], visibility=view, open_inbox=plane.open)

    assert (result.hidden, result.failed, result.delivered) == (1, 0, 0)
    assert not result.needs_retry
