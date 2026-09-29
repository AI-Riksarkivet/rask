"""B8: the cascade fires on the PUBLICATION, and the trigger carries the delta.

Two defects sit behind "the cascade moves no data", and they are independent:

* it woke on a lineage WRITE — a signal that says bytes landed and nothing about whether the quality
  gate accepted them, so the cascade could move data `published` had refused;
* the trigger named a TABLE, not a range, so a consumer had to rescan or keep its own bookmark, and a
  table's second arrival woke nothing useful.

And a third, quieter one: without `project` on the trigger the stage runner cannot resolve its tier roots,
falls back to the empty `MEDALLION_FROM_URI`/`MEDALLION_TO_URI`, and SKIPS its compute path — the
cascade "runs" and moves nothing at all.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from medallion.core.config import MedallionSettings
from medallion.services.publication_trigger import handle_publication


def _Settings() -> MedallionSettings:  # noqa: N802 — kept call-compatible with the stub it replaces
    """The REAL settings, not a hand-rolled stub — that stub is why the lane-name bug shipped.

    It carried exactly three attributes (`pubsub`, `bronze_topic`, `publish_timeout_seconds`), so the
    head could not read `bronze_namespace` even in principle, and the tests below happily asserted the
    CATALOG identifier was the lane name. A stub that cannot express the thing under test will always
    agree with whatever the code does.
    """
    return MedallionSettings(transform_routes=dict(ROUTES))


class _Dapr:
    def __init__(self, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self._fail = fail

    async def publish_event(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("broker unreachable")
        self.published.append(json.loads(kwargs["data"]))


#: The declared DAG, as the chart derives it from `medallion.stageRunners[]`. The head drives only lanes it
#: is told about — it used to stamp the bronze topic on every publication regardless of tier.
ROUTES = {"bronze": "medallion.bronze", "silver": "medallion.silver", "bronze-media": "medallion.media"}


def _event(action: str = "table_published", object_id: str = "table:acme-bronze$pages", **extra: Any) -> dict[str, Any]:
    """A publication as the CATALOG emits it: `<project>-<tier>$<table>`, with the tenant stated.

    The old default was `table:lane$pages`, from when segment 0 was believed to be the tenant. It is
    the NAMESPACE — `scripts/seed_estate.py` creates `acme-bronze`/`acme-silver`/`acme-gold` — and the
    project rides `extra.project` because no split can recover it.
    """
    if "project" not in extra:
        extra["project"] = "acme"
    elif extra["project"] is None:
        extra.pop("project")  # an estate with no tenant — expressible, not merely absent
    return {"data": {"action": action, "object_id": object_id, "event_id": "evt-1", "extra": extra}}


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
@pytest.mark.parametrize("event", [{}, _event(object_id="warehouse:acme"), _event(object_id="table:nodelimiter")])
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


@pytest.mark.asyncio
async def test_the_trigger_carries_the_CATALOG_VENDED_location() -> None:
    """I2 from the consuming end, and the reason the cascade moved nothing.

    The catalog vends a table at `s3://<warehouse>/<hash>_<ns>$<name>`; the stage runner composed
    `{project_root}/medallion/{namespace}` and read a path no catalog-written table has ever occupied.
    So the cascade fired correctly, woke the stage runner, and found an empty location — for every
    ingest-written table, silently.

    The fix is not for the stage runner to guess better. The catalog already HAS the location, so it puts it
    on the event and the trigger carries it; nothing downstream composes anything.
    """
    dapr = _Dapr()

    await handle_publication(dapr, _Settings(), _event(from_version=1, to_version=2, location="s3://lane-wh/abc123_lane$pages"))

    assert dapr.published[0]["from_uri"] == "s3://lane-wh/abc123_lane$pages"
