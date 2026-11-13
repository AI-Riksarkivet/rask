"""The CloudEvents envelope an event rides the bus in is derived from the event alone ([[XC-199]]).

A stream drops a message whose id it has seen inside its duplicate window, so two events sharing an id lose the later
one, and a relay's copy of an event has to carry the id its first publish carried. Every attribute is therefore read
off the event and nothing is minted at publish. The expected values are literals worked out from `docs/COMMIT-EVENTS.md`
§ The envelope on the bus, never the builders' own derivation.
"""

from __future__ import annotations

from typing import Any

import pytest

from lakehouse.cloud_events import control_envelope, lineage_envelope


RUN = "0190f3a2-7c1e-7b7a-9d2e-5a4c3b2a1f00"
TABLE = "3f6c1f0e-2b7d-4a55-9a51-0c8f4d2e7a11"
OTHER_TABLE = "9a0b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
PRODUCER = "https://producer.test/catalog"
CATALOG = "lance-namespace://lake.test/api/catalog"
RUN_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
DATASET_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/DatasetEvent"
INSTANT = "2026-11-13T09:30:00+00:00"
#: uuid5 in the namespace `docs/COMMIT-EVENTS.md` § An event's identity names, over
#: `https://producer.test/catalog|lance-namespace://lake.test/api/catalog|acme$bronze$events|drop_table|2026-11-13T09:30:00+00:00`.
DROP_IDENTITY = "ba031bec-403f-53f0-a34e-b566c467368c"


def _table(uuid: str | None = TABLE, **facet: str) -> dict[str, Any]:
    dataset: dict[str, Any] = {"namespace": CATALOG, "name": "acme$bronze$events"}
    lance = ({"tableUuid": uuid} if uuid else {}) | facet
    return dataset | ({"facets": {"rask_lance": lance}} if lance else {})


def _run(state: str = "COMPLETE", *, time: str = INSTANT, outputs: list[dict[str, Any]] | None = None, **members: Any) -> dict[str, Any]:
    event = {
        "eventType": state,
        "eventTime": time,
        "producer": PRODUCER,
        "schemaURL": RUN_SCHEMA,
        "run": {"runId": RUN},
        "job": {"namespace": "lance-catalog", "name": "insert"},
    }
    return event | {"outputs": [_table()] if outputs is None else outputs} | members


_A_WRITE = {
    "specversion": "1.0",
    "id": f"{RUN}@COMPLETE",
    "source": PRODUCER,
    "type": "rask.lineage.run.complete.v1",
    "subject": f"table:{TABLE}",
    "time": "2026-11-13T09:30:00Z",
    "datacontenttype": "application/json",
    "dataschema": RUN_SCHEMA,
}


def _without(attributes: dict[str, str], *absent: str, **changed: str) -> dict[str, str]:
    return {name: value for name, value in (attributes | changed).items() if name not in absent}


@pytest.mark.parametrize(
    ("event", "attributes"),
    [
        pytest.param(_run(), _A_WRITE, id="a-write-of-one-governed-table"),
        pytest.param(
            _run("RUNNING"),
            _A_WRITE | {"id": f"{RUN}@RUNNING@{INSTANT}", "type": "rask.lineage.run.running.v1"},
            id="a-state-that-ends-nothing-is-told-apart-by-its-time",
        ),
        pytest.param(
            _run(eventType=None),
            _A_WRITE | {"id": f"{RUN}@OTHER@{INSTANT}", "type": "rask.lineage.run.other.v1"},
            id="a-run-event-declaring-no-state-is-openlineages-other",
        ),
        pytest.param(
            {"eventTime": INSTANT, "producer": PRODUCER, "schemaURL": DATASET_SCHEMA, "dataset": _table(operation="drop_table")},
            _A_WRITE | {"id": f"{DROP_IDENTITY}@COMPLETE", "type": "rask.lineage.dataset.v1", "dataschema": DATASET_SCHEMA},
            id="a-dataset-change-under-its-derived-identity",
        ),
        pytest.param(_run(outputs=[_table(), _table(OTHER_TABLE)]), _without(_A_WRITE, "subject"), id="several-outputs-name-no-one-subject"),
        pytest.param(_run(outputs=[_table(None)]), _without(_A_WRITE, "subject"), id="an-output-no-uuid-names-is-no-subject"),
        pytest.param(_run(time="2026-11-13T09:30:00"), _without(_A_WRITE, "time"), id="a-time-with-no-offset-is-no-instant"),
        pytest.param(_run(time="yesterday"), _without(_A_WRITE, "time"), id="a-time-that-does-not-parse-is-left-out"),
        pytest.param(_run(producer=None), _A_WRITE | {"source": "rask/lineage"}, id="an-event-naming-no-producer"),
        pytest.param(_run(schemaURL=None), _without(_A_WRITE, "dataschema"), id="an-event-naming-no-schema"),
    ],
)
def test_a_lineage_event_is_enveloped_from_what_it_carries(event: dict[str, Any], attributes: dict[str, str]) -> None:
    assert lineage_envelope(event).as_wire() == attributes | {"data": event}


def test_an_envelope_hands_its_event_on_as_it_was_given() -> None:
    """Never re-serialized: the event is its producer's document, and a JSON-mode dump rewrites what JSON cannot say."""
    event = _run(ratio=float("inf"))

    assert lineage_envelope(event).as_wire()["data"] == event


@pytest.mark.parametrize(
    "occurred_at",
    [pytest.param("2026-11-13T09:30:00Z", id="an-instant"), pytest.param("2026-11-13T09:30:00", id="a-time-with-no-offset-is-utc-as-the-event-says")],
)
def test_a_control_event_is_enveloped_from_what_it_carries(occurred_at: str) -> None:
    """Its data is the event as signed: the members it holds as null and its signature ride unchanged, since a
    verifying door canonicalises what arrives and a dropped member is another document."""
    event = {
        "event_id": "7d3c0a1e5b9f4c2d8e6a1b0c9d8e7f6a",
        "occurred_at": occurred_at,
        "action": "grant_added",
        "object_type": "grant",
        "object_id": f"table:{TABLE}",
        "object_name": "acme$bronze$events",
        "object_uuid": TABLE,
        "namespace_uuid": None,
        "actor": None,
        "extra": {"relation": "reader", "subject": "user:dex~alice"},
        "rask_signature": {"identity": "rask-catalog", "kid": "k1", "sig": "c2ln"},
    }

    assert control_envelope(event).as_wire() == {
        "specversion": "1.0",
        "id": "7d3c0a1e5b9f4c2d8e6a1b0c9d8e7f6a",
        "source": "rask/catalog",
        "type": "rask.catalog.control.grant_added.v1",
        "subject": f"table:{TABLE}",
        "time": "2026-11-13T09:30:00Z",
        "datacontenttype": "application/json",
        "data": event,
    }
