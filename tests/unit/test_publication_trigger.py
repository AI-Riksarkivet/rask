"""B8: the cascade fires on the PUBLICATION, and the trigger carries the delta.

Two defects sit behind "the cascade moves no data", and they are independent:

* it woke on a lineage WRITE — a signal that says bytes landed and nothing about whether the quality
  gate accepted them, so the cascade could move data `published` had refused;
* the trigger named a TABLE, not a range, so a consumer had to rescan or keep its own bookmark, and a
  table's second arrival woke nothing useful.

And a third, quieter one: without `project` on the trigger the mover cannot resolve its tier roots,
falls back to the empty `MEDALLION_FROM_URI`/`MEDALLION_TO_URI`, and SKIPS its compute path — the
cascade "runs" and moves nothing at all.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from medallion.services.publication_trigger import handle_publication


class _Settings:
    pubsub = "lineage-pubsub"
    bronze_topic = "medallion.bronze"
    publish_timeout_seconds = 5.0


class _Dapr:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self._fail = fail

    async def publish_event(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("broker unreachable")
        self.published.append(json.loads(kwargs["data"]))


def _event(action: str = "table_published", object_id: str = "table:lane$pages", **extra: Any) -> dict[str, Any]:
    return {"data": {"action": action, "object_id": object_id, "event_id": "evt-1", "extra": extra}}


@pytest.mark.asyncio
async def test_a_publication_triggers_the_cascade_WITH_the_range() -> None:
    """The whole of D-R3 on the wire: the trigger names which rows are new, not merely that some are."""
    dapr = _Dapr()

    result = await handle_publication(dapr, _Settings(), _event(from_version=3, to_version=4))

    assert result == {"status": "SUCCESS"}
    assert len(dapr.published) == 1
    trigger = dapr.published[0]
    assert (trigger["from_version"], trigger["to_version"]) == (3, 4)
    assert trigger["dataset"] == "lane$pages"


@pytest.mark.asyncio
async def test_the_trigger_carries_the_PROJECT_or_the_mover_moves_nothing() -> None:
    """The quiet half of B8.

    `handle_stage` resolves its tier roots only when the trigger carries a project; otherwise it uses
    `MEDALLION_FROM_URI`/`MEDALLION_TO_URI`, which default to empty, and its `if compute_enabled and
    from_uri and to_uri` guard silently skips the compute path. A cascade that runs and moves nothing
    looks identical to one that had nothing to move.
    """
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(from_version=None, to_version=1))

    assert dapr.published[0]["project"] == "lane"


@pytest.mark.asyncio
async def test_a_FIRST_publication_carries_a_null_from_rather_than_zero() -> None:
    """ "No prior publication" and "published from version 0" are different claims.

    Coercing None to 0 would make a first publication indistinguishable from one that followed a
    version nobody published, and the consumer's filter (`> from`) would silently change meaning.
    """
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(from_version=None, to_version=2))

    assert dapr.published[0]["from_version"] is None
    assert dapr.published[0]["to_version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _event(action="grant_added"),
        _event(action="table_created"),
        _event(action="warehouse_created"),
    ],
)
async def test_a_GOVERNANCE_notice_drives_nothing(event: dict[str, Any]) -> None:
    """The control topic carries every catalog mutation. Only a publication means data is READY —
    firing the cascade on a grant or a table create is the whole-table-granularity mistake again."""
    dapr = _Dapr()

    result = await handle_publication(dapr, _Settings(), event)

    assert result == {"status": "SUCCESS"}, "an event we ignore must be ACKED, not redelivered forever"
    assert dapr.published == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [{}, {"data": "not-a-dict"}, _event(object_id="warehouse:acme"), _event(object_id="table:nodelimiter")])
async def test_an_unparseable_event_is_ACKED_not_retried(event: dict[str, Any]) -> None:
    """A head that RETRYs on events it can never handle turns one malformed message into a permanent
    hot loop against the broker."""
    dapr = _Dapr()

    assert await handle_publication(dapr, _Settings(), event) == {"status": "SUCCESS"}
    assert dapr.published == []


@pytest.mark.asyncio
async def test_a_publish_OUTAGE_retries() -> None:
    """The one failure a redelivery can actually fix — and the one thing that must not be acked away,
    because the publication really did happen and the cascade really has not been told."""
    dapr = _Dapr(fail=True)

    assert await handle_publication(dapr, _Settings(), _event(from_version=1, to_version=2)) == {"status": "RETRY"}
