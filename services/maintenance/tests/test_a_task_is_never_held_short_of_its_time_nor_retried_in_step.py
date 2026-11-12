"""What the work queue tells the bus about time: when a held task fires, and when a failed one comes again.

A held task fires at an ``@at`` schedule, which ADR-51 spells to the second. A time between two seconds is held to the
second after it, never the one before: a trash record's task holds itself until its release, and one fired a fraction
of a second short of it finds the release still ahead and holds itself again, at once, until the clock passes it.

A failed delivery is asked again after a delay that grows with each delivery up to a ceiling, drawn at random beneath
that bound (full jitter): tasks that failed together, on one store outage, would otherwise all come back in the same
second, delivery after delivery.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from maintenance.services.queue import at_schedule, redelivery_delay


@pytest.mark.parametrize(
    ("at", "held"),
    [
        pytest.param(datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC), "@at 2026-11-12T10:00:00Z", id="on-a-second"),
        pytest.param(datetime(2026, 11, 12, 10, 0, 0, 1, tzinfo=UTC), "@at 2026-11-12T10:00:01Z", id="past-a-second"),
    ],
)
def test_a_held_time_is_never_short_of_the_instant_asked_for(at: datetime, held: str) -> None:
    assert at_schedule(at) == held


def test_a_failed_deliverys_retry_is_drawn_beneath_its_growing_bound() -> None:
    random.seed(20261112)

    third = [redelivery_delay(3) for _ in range(200)]
    late = [redelivery_delay(40) for _ in range(200)]

    assert (min(third) < 15.0, max(third) > 45.0, max(third) <= 60.0) == (True, True, True), third
    assert (min(late) < 225.0, max(late) > 675.0, max(late) <= 900.0) == (True, True, True), late
