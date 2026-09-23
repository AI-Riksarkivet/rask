"""A catalog change no job performed is ingested as a ``DatasetEvent``, not as a run that never ran.

[[LIN-004]], ruled in `docs/DECISIONS.md` § D (2026-09-21): the OpenLineage spec defines `DatasetEvent`
as "A Dataset sent within static metadata events" and its schema forbids the `job` and `run` members
outright (`"not": { "required": ["job", "run"] }`), so a DDL change is exactly what it is for. Verified
against the installed client: `DatasetEvent` carries `eventTime`, `producer`, `schemaURL` and `dataset`
— no `eventType`, no `run`, no `job`.

THE AUTHOR IS NOT THE `ownership` FACET, and that is the security half of this change. `enforce_bus_authz`
authorizes a bus-delivered event as the subject the producer STAMPED, read by `author_sub_from_payload`
— which takes `sub` and refuses `name`/`ownership` because those are producer-supplied attribution and
would let a producer authorize itself under someone else's display name. Moving DDL off the run must not
move its author onto a facet the authorizer is required to distrust, so the verified sub rides rask's own
reserved `author` facet on the DATASET, and `ownership` may ride beside it for external consumers only.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from lineage.models import DatasetEvent, author_sub_from_payload


def _payload(**overrides: object) -> dict[str, object]:
    """A spec-shaped DatasetEvent for a `create_table`, with rask's reserved author facet."""
    payload: dict[str, object] = {
        "eventTime": "2026-09-23T10:00:00Z",
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/DatasetEvent",
        "dataset": {
            "namespace": "lance-catalog",
            "name": "bronze$events",
            "facets": {
                "author": {"sub": "auth0|alice", "name": "Alice"},
                "lifecycleStateChange": {"lifecycleStateChange": "CREATE"},
                "lance": {"operation": "create_table", "version": 1},
            },
        },
    }
    payload.update(overrides)
    return payload


def test_a_dataset_event_parses_without_a_run_or_a_job() -> None:
    """The whole point: there is no run to name and no job that performed it."""
    event = DatasetEvent.model_validate(_payload())
    assert event.dataset.name == "bronze$events"
    assert event.operation == "create_table"


def test_a_payload_carrying_a_run_or_a_job_is_REFUSED() -> None:
    """The spec forbids both members; accepting one would re-admit the phantom through the new door."""
    for forbidden in ({"run": {"runId": "r1"}}, {"job": {"namespace": "n", "name": "j"}}):
        with pytest.raises(ValidationError):
            DatasetEvent.model_validate(_payload(**forbidden))


def test_the_authorizer_reads_the_verified_sub_off_the_dataset() -> None:
    """`enforce_bus_authz` authorizes as whatever this returns — a DDL event must not become unauthored."""
    assert author_sub_from_payload(_payload()) == "auth0|alice"


def test_the_authorizer_still_refuses_the_ownership_FACET_as_a_source() -> None:
    """`ownership` is producer-supplied attribution. Reading it here would let a producer sign as anyone."""
    unverified = _payload()
    dataset = unverified["dataset"]
    assert isinstance(dataset, dict)
    dataset["facets"] = {"ownership": {"owners": [{"name": "auth0|mallory", "type": "MAINTAINER"}]}}
    assert author_sub_from_payload(unverified) is None


def test_a_dataset_event_presents_the_dataset_as_its_OUTPUT() -> None:
    """`enforce_output_authz` gates on outputs; a DDL change must be gated on the table it changed."""
    event = DatasetEvent.model_validate(_payload())
    assert [d.name for d in event.outputs] == ["bronze$events"]
    assert event.inputs == []
    assert event.run_id is None
