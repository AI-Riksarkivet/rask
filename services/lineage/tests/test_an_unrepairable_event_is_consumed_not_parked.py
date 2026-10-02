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

from collections.abc import Mapping
from typing import Any, cast

import pytest

from lineage.core.metrics import Door
from lineage.models import DatasetEvent, RunEvent, UnauthoredRunError
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
    """A repository that records what it was asked to ingest.

    Typed on `RunEvent` rather than `Any` because that IS the contract under test: every arm below
    asserts the event did or did not reach the graph, and a fake that accepts anything cannot fail
    when the door starts handing it something else.
    """

    def __init__(self) -> None:
        self.ingested: list[RunEvent] = []

    async def ingest_event(self, event: RunEvent) -> None:
        self.ingested.append(event)


@pytest.mark.asyncio
async def test_a_MALFORMED_payload_is_acked_not_parked() -> None:
    """It cannot be repaired by anything, so a dead-letter copy is a duplicate with no reader."""
    repo = _Repo()

    assert await handle_cloud_event(cast(Any, repo), {"data": {"not": "an event"}}, door=Door.SUBSCRIBER) == {"status": "SUCCESS"}
    assert repo.ingested == []


@pytest.mark.asyncio
async def test_an_UNAUTHORED_run_is_acked_not_parked() -> None:
    """36 re-parks of one run across restarts, measured. The author cannot be added after publication."""

    async def refuse_unauthored(_event: RunEvent | DatasetEvent, _arrived: Mapping[str, Any]) -> None:
        # The TYPE is what carries the distinction, not the message — the gate raises this one, and a
        # test that hand-rolled a plain `PermissionDeniedError` here would be asserting on a string.
        raise UnauthoredRunError("a bus-delivered run must carry a verified author sub to be authorized")

    repo = _Repo()

    assert await handle_cloud_event(cast(Any, repo), _event(), refuse_unauthored, door=Door.SUBSCRIBER) == {"status": "SUCCESS"}
    assert repo.ingested == []
