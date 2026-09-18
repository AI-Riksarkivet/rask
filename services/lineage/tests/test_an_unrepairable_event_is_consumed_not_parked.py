"""An event no grant and no redelivery could ever accept is CONSUMED, not parked forever.

MEASURED on the deployed estate 2026-09-18, one hour spanning one lineage roll: 44
`lineage_event_unauthorized`, 44 `dapr_dead_letter_parked`, and 37 of the 44 refusals carried the same
reason — "a bus-delivered run must carry a verified author sub to be authorized" — for ONE run id,
`44f082a9-…`, in a single burst at pod start with `already_recorded=False`. The subscriber is ephemeral
with `deliverPolicy: all` (`chart/templates/dapr-component.yaml`), so every restart meets the retained
stream again, refuses the same unrepairable events again, and appends a NEW dead-letter message about
an event the DLQ already holds. The cost is a growth curve, not one event.

THE SPLIT IS "COULD ANYTHING EVER CHANGE THE ANSWER", and it is the only line that separates the two
honestly:

* A payload that does not parse, and a run that carries no author at all, are defects IN THE MESSAGE.
  No tuple, no redelivery and no restart can add an author to bytes already published, so re-presenting
  them buys nothing and parking them buys a duplicate. These are ACKED — counted, never silently
  dropped, and still on the stream for its retention.
* A named PERSON who lacks a grant is a different fact. A grant can be written, and the same event then
  succeeds on the next presentation, so it keeps the DROP that routes it to the dead-letter topic.

The unauthored class stops being unrepairable when the producers sign ([[LH-064]]) — at which point
this arm should stop firing rather than start discarding more.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from lineage.models import UnauthoredRunError
from lineage.services.consumer import handle_cloud_event


def _event(run_id: str = "11111111-2222-3333-4444-555555555555") -> dict[str, Any]:
    return {
        "data": {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-18T20:00:00Z",
            "producer": "https://example.invalid/producer",
            "job": {"namespace": "rask", "name": "a-job"},
            "run": {"runId": run_id},
        }
    }


class _Repo:
    def __init__(self) -> None:
        self.ingested: list[Any] = []

    async def ingest_event(self, event: Any) -> None:
        self.ingested.append(event)


@pytest.mark.asyncio
async def test_a_MALFORMED_payload_is_acked_not_parked() -> None:
    """It cannot be repaired by anything, so a dead-letter copy is a duplicate with no reader."""
    repo = _Repo()

    assert await handle_cloud_event(cast(Any, repo), {"data": {"not": "an event"}}) == {"status": "SUCCESS"}
    assert repo.ingested == []


@pytest.mark.asyncio
async def test_an_UNAUTHORED_run_is_acked_not_parked() -> None:
    """36 re-parks of one run across restarts, measured. The author cannot be added after publication."""

    async def refuse_unauthored(_event: Any) -> None:
        # The TYPE is what carries the distinction, not the message — the gate raises this one, and a
        # test that hand-rolled a plain `PermissionDeniedError` here would be asserting on a string.
        raise UnauthoredRunError("a bus-delivered run must carry a verified author sub to be authorized")

    repo = _Repo()

    assert await handle_cloud_event(cast(Any, repo), _event(), refuse_unauthored) == {"status": "SUCCESS"}
    assert repo.ingested == []


@pytest.mark.asyncio
async def test_a_PERSON_without_a_grant_still_parks() -> None:
    """The half that must not move: a grant can be written, and then the event is accepted.

    Discarding this one would delete provenance that a tuple away is perfectly recordable — which is
    exactly the silent loss the dead-letter topic exists to prevent.
    """

    async def refuse_person(_event: Any) -> None:
        raise PermissionDeniedError("can_write_data required on outputs: acme-ns$t1")

    repo = _Repo()

    assert await handle_cloud_event(cast(Any, repo), _event(), refuse_person) == {"status": "DROP"}
    assert repo.ingested == []


@pytest.mark.asyncio
async def test_an_UNAVAILABLE_authorizer_still_retries() -> None:
    """An outage is not a verdict. Acking here would delete provenance for its duration."""

    async def boom(_event: Any) -> None:
        raise RuntimeError("authorization service is not available")

    repo = _Repo()

    assert await handle_cloud_event(cast(Any, repo), _event(), boom) == {"status": "RETRY"}
