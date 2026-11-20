"""An event no grant and no redelivery could ever accept is TAKEN, not parked.

MEASURED on the deployed estate 2026-09-18, one hour spanning one lineage roll: 44 refusals and 44 parks, and 37 of
the 44 refusals carried the same reason — "a bus-delivered run must carry a verified author sub to be authorized" — for
ONE run id, `44f082a9-…`, in a single burst at pod start with `already_recorded=False`. Lineage's consumer starts at the
stream's first message, so every replay meets the retained stream again and refuses the same unrepairable events again;
a park of each would keep a new copy every time. The cost is a growth curve, not one event.

THE SPLIT IS "COULD ANYTHING EVER CHANGE THE ANSWER", and it is the only line that separates the two honestly:

* A payload that does not parse, and a run that carries no author at all, are defects IN THE MESSAGE. No tuple, no
  redelivery and no restart can add an author to bytes already published, so re-presenting them buys nothing and
  parking them buys a duplicate. They are TAKEN: counted, never silently dropped, and still on the stream for its
  retention.
* A named PERSON who lacks a grant is a different fact. A grant can be written, and the same event then succeeds on its
  next presentation, so it is parked where the estate admin can replay it.

THE LANE READS EVERY EVENT LINEAGE'S MODEL ACCEPTS. The bus seam reads no body nested deeper than
`lakehouse.events.bus_delivery.MAX_ENVELOPE_DEPTH` as an envelope, and lineage's HTTP door stages whatever its model accepts.
An event the model accepted and the lane did not read would be answered 202 at the door and taken here unrecorded, so
the two bounds are held together from both sides.

Driven through lineage's own app and lifespan, each event delivered to its ingest lane.
"""

from __future__ import annotations

from typing import Any

import pytest
from lineage_lane import Feed, governed, started
from pydantic import ValidationError

from lakehouse.events.bus_delivery import MAX_ENVELOPE_DEPTH, Outcome, Settlement
from lineage.services.provenance import parse_event


#: How far beneath its envelope a run's facet lies: the envelope, its event, the run and the run's facets.
_LEVELS_ABOVE_A_FACET = 4
#: The deepest facet lineage's event model accepts, in lists (measured 2026-11-14 on the locked pydantic, which refuses
#: to serialise a deeper one): an envelope 258 deep.
_DEEPEST_FACET_THE_MODEL_ACCEPTS = 254


def _event(run_id: str = "11111111-2222-3333-4444-555555555555", *, facet_lists: int = 0) -> dict[str, Any]:
    """A run's event in its envelope, its run carrying a facet nested ``facet_lists`` lists deep when asked for one."""
    facet: list[Any] = []
    for _ in range(facet_lists - 1):
        facet = [facet]
    return {
        "data": {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-18T20:00:00Z",
            "producer": "https://example.invalid/producer",
            "job": {"namespace": "rask", "name": "a-job"},
            "run": {"runId": run_id, **({"facets": {"nested": facet}} if facet_lists else {})},
        }
    }


def test_a_malformed_payload_is_taken_not_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    """It cannot be repaired by anything, so a kept copy is a duplicate with no reader."""
    with started(monkeypatch, Feed()) as lineage:
        settled = lineage.deliver({"data": {"not": "an event"}})

    assert settled == Settlement(outcome=Outcome.TAKEN)
    assert (lineage.feed.recorded, lineage.feed.kept) == ([], {})


def test_an_unauthored_run_is_taken_not_parked(monkeypatch: pytest.MonkeyPatch) -> None:
    """36 re-parks of one run across restarts, measured. The author cannot be added after publication."""
    governed(monkeypatch)
    with started(monkeypatch, Feed()) as lineage:
        settled = lineage.deliver(_event())

    assert settled == Settlement(outcome=Outcome.TAKEN)
    assert (lineage.feed.recorded, lineage.feed.kept) == ([], {})


def test_the_deepest_event_the_model_accepts_is_recorded_off_the_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    with started(monkeypatch, Feed()) as lineage:
        settled = lineage.deliver(_event(facet_lists=_DEEPEST_FACET_THE_MODEL_ACCEPTS))

    assert settled == Settlement(outcome=Outcome.TAKEN)
    assert len(lineage.feed.recorded) == 1


def test_an_event_nested_past_what_the_lane_reads_is_one_the_model_refuses() -> None:
    one_past_the_lane = _event(facet_lists=MAX_ENVELOPE_DEPTH + 1 - _LEVELS_ABOVE_A_FACET)

    with pytest.raises(ValidationError, match="depth exceeded"):
        parse_event(one_past_the_lane["data"])
