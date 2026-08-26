"""An unreadable watch index was indistinguishable from a project with no watchers.

`watchers_of` swallowed EVERY fault and returned `[]`, so `audience_for` degraded to v1's audience
and `ingest_run_event` acked DAPR_SUCCESS. The author was told; the watchers were not; and the bus
lane was finished with that event forever.

The docstring's reason for swallowing is the part that does not hold: "a watcher-index outage does
not heal on redelivery". For the fault class it actually covers -- a sidecar restart, an actor
rebalance, a state-store failover -- it heals in seconds. And a retry is BOUNDED here, not a loop:
the subscription registers a `deadLetterTopic`, so an event that keeps failing parks visibly instead
of redelivering forever. `audience_for` already documents that a raising resolver is a supported
shape ("callers that cannot tolerate that pass a resolver which absorbs its own faults").

The reconciler does re-offer most of these -- an independent lane with its own cursor -- so the
residual this closes is narrow: an outage spanning both the bus delivery and every reconciler pass
covering that seq. Narrow is not nothing, and the cost of the fix is one counted duplicate for the
author on a retry, because the fan-out is idempotent on the notification's natural key.

WHAT STAYS SWALLOWED: a `ValueError` from `watch_index_for`, which is an unusable project id. No
retry fixes that, and answering RETRY would park a permanently-bad event in the DLQ instead of
delivering the author's own notification.
"""

from __future__ import annotations

from typing import Any

import pytest

from notifications import proxies
from notifications.proxies import WatchIndexUnavailable, watchers_of


@pytest.mark.asyncio
async def test_a_TRANSPORT_fault_is_raised_so_the_handler_can_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WEDGE. Returning [] here is the bus lane deciding this project has no watchers."""

    class _Down:
        async def list_watchers(self) -> dict[str, Any]:
            raise ConnectionError("actor host is rebalancing")

    monkeypatch.setattr(proxies, "watch_index_for", lambda _p: _Down())

    with pytest.raises(WatchIndexUnavailable):
        await watchers_of("acme")


@pytest.mark.asyncio
async def test_an_UNUSABLE_project_id_is_still_absorbed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permanent, so retrying is worse than degrading: it would park the AUTHOR's own notification in
    the DLQ over a watcher lookup that can never succeed."""

    def _refuse(_project: str) -> Any:
        raise ValueError("a watch index requires a non-empty project id")

    monkeypatch.setattr(proxies, "watch_index_for", _refuse)

    assert await watchers_of("") == []


@pytest.mark.asyncio
async def test_a_project_with_NO_watchers_still_answers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case the swallow used to be indistinguishable from. It must stay an ordinary empty answer."""

    class _Empty:
        async def list_watchers(self) -> dict[str, Any]:
            return {"subjects": []}

    monkeypatch.setattr(proxies, "watch_index_for", lambda _p: _Empty())

    assert await watchers_of("acme") == []


@pytest.mark.asyncio
async def test_the_handler_answers_RETRY_rather_than_acking_a_partial_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point, at the seam that decides. Bounded by the subscription's deadLetterTopic, so a
    fault that does not heal parks visibly instead of redelivering forever."""
    from datetime import UTC, datetime

    from notifications.api.fanout import audience_for
    from notifications.api.lineage_events import Notifiable
    from notifications.models import InboxPointer, NotificationReason

    class _Down:
        async def list_watchers(self) -> dict[str, Any]:
            raise ConnectionError("state store failover")

    monkeypatch.setattr(proxies, "watch_index_for", lambda _p: _Down())

    notice = Notifiable(
        delivery=InboxPointer(
            notification_id="run-1@COMPLETE",
            reason=NotificationReason.AUTHOR,
            object_id="silver$pages",
            source_run_id="run-1",
            occurred_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
        ),
        author="CiQwOGE4Njg0Yi1kYjg4",
        project="acme",
        outputs=frozenset({"silver$pages"}),
    )

    with pytest.raises(WatchIndexUnavailable):
        await audience_for(notice, watchers=watchers_of)


class TestMembershipIsRECHECKEDAtDelivery:
    """`project#member` gates watch CREATION, and three docstrings asserted it was re-checked at
    delivery. None of them was true.

    For most subjects the visibility gate caught a revocation anyway, because the FGA model routes
    membership into readership (`warehouse#reader: ... or member from project`, inherited down to
    `table#can_be_notified`). The residue is a subject holding an INDEPENDENT reader grant on the
    output: they kept receiving `reason: watch` rows for a project they were offboarded from,
    indefinitely -- only they can delete their own watch.

    The finding offered "implement the re-check or delete the claim". Implementing it is what the
    docstrings already committed to, so the claims become true rather than being walked back.
    """

    @staticmethod
    def _notice() -> Any:
        from datetime import UTC, datetime

        from notifications.api.lineage_events import Notifiable
        from notifications.models import InboxPointer, NotificationReason

        return Notifiable(
            delivery=InboxPointer(
                notification_id="run-1@COMPLETE",
                reason=NotificationReason.AUTHOR,
                object_id="silver$pages",
                source_run_id="run-1",
                occurred_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
            ),
            author="alice",
            project="acme",
            outputs=frozenset({"silver$pages"}),
        )

    @pytest.mark.asyncio
    async def test_an_OFFBOARDED_watcher_is_dropped_from_the_audience(self) -> None:
        """THE WEDGE. Nothing re-asked, so the watch outlived the membership that justified it."""
        from notifications.api.fanout import audience_for

        async def _watchers(_project: str) -> list[str]:
            return ["bob"]

        async def _never_a_member(_subject: str, _project: str) -> bool:
            return False

        audience = await audience_for(self._notice(), watchers=_watchers, members=_never_a_member)

        assert audience == ("alice",), f"an offboarded watcher stayed in the audience: {audience}"

    @pytest.mark.asyncio
    async def test_a_CURRENT_member_is_still_delivered_to(self) -> None:
        from notifications.api.fanout import audience_for

        async def _watchers(_project: str) -> list[str]:
            return ["bob"]

        async def _is_a_member(_subject: str, _project: str) -> bool:
            return True

        assert await audience_for(self._notice(), watchers=_watchers, members=_is_a_member) == ("alice", "bob")

    @pytest.mark.asyncio
    async def test_an_AUTHORIZATION_outage_raises_rather_than_silently_dropping(self) -> None:
        """A silent False is indistinguishable from a revoked membership -- the exact conflation this
        whole change is about. Raising lets the handler answer RETRY."""
        from notifications.api.fanout import audience_for
        from service_kit.exceptions import ServiceUnavailableError

        async def _watchers(_project: str) -> list[str]:
            return ["bob"]

        async def _down(_subject: str, _project: str) -> bool:
            raise ServiceUnavailableError("authorization is enabled but unavailable")

        with pytest.raises(ServiceUnavailableError):
            await audience_for(self._notice(), watchers=_watchers, members=_down)

    @pytest.mark.asyncio
    async def test_the_AUTHOR_is_never_membership_checked(self) -> None:
        """You may always be told about your own run -- the author needs no registry and no permission.
        Checking them would make a run's own author droppable, which no docstring ever claimed."""
        from notifications.api.fanout import audience_for

        asked: list[str] = []

        async def _watchers(_project: str) -> list[str]:
            return []

        async def _record(subject: str, _project: str) -> bool:
            asked.append(subject)
            return True

        assert await audience_for(self._notice(), watchers=_watchers, members=_record) == ("alice",)
        assert asked == [], f"the author was membership-checked: {asked}"
