"""The ingress matrix: DROP, RETRY, SUCCESS — and the audience rules behind each answer.

The statuses are not three flavours of "handled". DROP is the only answer to a payload redelivery
cannot fix, RETRY is the only answer to a dependency that was briefly unreachable, and getting them
the wrong way round is how a subscription stops delivering anything at all. Each case below names
which of the two it is holding in place.
"""

from typing import TYPE_CHECKING, Any, cast

import pytest

from notifications.api.ingest import ingest_run_event
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


# --- SUCCESS with delivery ---------------------------------------------------------------------


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
