"""Lineage pulls its ingest consumer with the numbers its settings name ([[XC-199]]).

How many deliveries a message is handled on, how long one asked for again waits, the consumer's ack wait and how many
deliveries a replica handles at once are the chart's to set (`services.lineage.delivery`,
`services.lineage.deliveriesInFlight`), and reach the pod as `LINEAGE_DELIVERY_*` and `LINEAGE_DELIVERIES_IN_FLIGHT`.
A lane built from anything else would settle by numbers nobody set: a message parked sooner or later than the alert
on it assumes, or handled past the ack wait the stream Job declared, so that the bus hands it to another replica
beneath its handler.

Driven through lineage's own lifespan, which hands the lane to its bus as a pod's does (`lineage_lane.started`).
"""

from __future__ import annotations

import pytest
from lineage_lane import Feed, started

from lakehouse.bus_delivery import Lane


def test_lineage_pulls_its_consumer_with_the_numbers_its_settings_name(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "LINEAGE_DELIVERY_ATTEMPTS": "2",
        "LINEAGE_DELIVERY_RETRY_SECONDS": "7",
        "LINEAGE_DELIVERY_ACK_WAIT_SECONDS": "45",
        "LINEAGE_DELIVERIES_IN_FLIGHT": "3",
    }.items():
        monkeypatch.setenv(name, value)

    with started(monkeypatch, Feed()) as lineage:
        pulled = lineage.lane()

    assert pulled == Lane(stream="LINEAGE", consumer="lineage-ingest", attempts=2, retry_seconds=7.0, ack_wait_seconds=45.0, in_flight=3)
