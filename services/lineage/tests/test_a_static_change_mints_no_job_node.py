"""A ``DatasetEvent`` reaches the graph by its own door, and the door that mints Job nodes never sees it.

[[LIN-004]]. `build_write_event` wraps every catalog operation in a synthetic run, so a DDL change
creates a `(:Run)` that never executed and a `(:Job)` that never ran. The job name is per-table-per-
operation, so the phantom population grows with the TABLE count rather than with work — and the
`/jobs` governance fold makes a Job's output set its access handle, which makes each phantom an
access-control object for an operation nobody performed.

The routing is what stops that: a payload carrying `dataset` and no `run` goes to the dataset door,
which merges the table, records who originated it, and writes the feed row — and never touches
`MERGE_JOB` or `MERGE_RUN`.

THE FEED ROW STILL NEEDS AN IDENTITY. Its dedup indexes key on `(run_id, event_type, ...)` and SQL
NULL never equals NULL, so a row carrying neither is appended again on every at-least-once redelivery.
`service_kit.openlineage.static_event_id` supplies one derived from the event, and the feed's own
`event_type` column reads COMPLETE because a static fact has no other state — the STORED event stays
spec-correct and carries no `eventType` at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from lineage.models import DatasetEvent, RunEvent
from lineage.services.consumer import handle_cloud_event


def _static_payload() -> dict[str, Any]:
    return {
        "eventTime": "2026-09-23T10:00:00+00:00",
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "dataset": {
            "namespace": "lance-catalog",
            "name": "silver$features",
            "facets": {
                "author": {"sub": "auth0|alice", "name": "Alice"},
                "lance": {"operation": "create_table", "version": 1},
            },
        },
    }


class _Repo:
    """Records which door an event reached. Two lists, because the whole point is that they differ."""

    def __init__(self) -> None:
        self.runs: list[RunEvent] = []
        self.datasets: list[DatasetEvent] = []

    async def ingest_event(self, event: RunEvent) -> None:
        self.runs.append(event)

    async def ingest_dataset_event(self, event: DatasetEvent) -> None:
        self.datasets.append(event)


@pytest.mark.asyncio
async def test_a_static_metadata_event_reaches_the_dataset_door() -> None:
    """And NOT the run door, which is the one that mints a Job node per table per operation."""
    repo = _Repo()

    assert await handle_cloud_event(cast(Any, repo), {"data": _static_payload()}) == {"status": "SUCCESS"}

    assert [event.dataset.name for event in repo.datasets] == ["silver$features"]
    assert repo.runs == [], "a static metadata change reached the run door and will mint a phantom Job"


@pytest.mark.asyncio
async def test_a_run_event_still_reaches_the_run_door() -> None:
    """The discrimination must not move real runs: a run carries `run` and `job` and belongs there."""
    repo = _Repo()
    payload = {
        "eventType": "COMPLETE",
        "eventTime": "2026-09-23T10:00:00+00:00",
        "run": {"runId": "11111111-2222-5333-8444-555555555555", "facets": {"author": {"sub": "auth0|alice"}}},
        "job": {"namespace": "lance-medallion", "name": "aggregate_gold"},
        "outputs": [{"namespace": "gold", "name": "gold$catalog"}],
    }

    assert await handle_cloud_event(cast(Any, repo), {"data": payload}) == {"status": "SUCCESS"}

    assert [event.job.name for event in repo.runs] == ["aggregate_gold"]
    assert repo.datasets == []


@pytest.mark.asyncio
async def test_a_static_event_is_AUTHORIZED_before_it_reaches_the_graph() -> None:
    """The dataset door is not a way around the check every run event passes."""
    repo = _Repo()
    seen: list[str | None] = []

    async def authorize(event: RunEvent | DatasetEvent, _arrived: Mapping[str, Any]) -> None:
        seen.append(event.run_id)

    assert await handle_cloud_event(cast(Any, repo), {"data": _static_payload()}, authorize=authorize) == {"status": "SUCCESS"}
    assert seen == [None], "the static event bypassed authorization on its way to the graph"


@pytest.mark.asyncio
async def test_a_payload_carrying_BOTH_a_dataset_and_a_run_is_refused() -> None:
    """The spec forbids the pairing; accepting it would re-admit the phantom through the new door."""
    repo = _Repo()
    payload = {**_static_payload(), "run": {"runId": "11111111-2222-5333-8444-555555555555"}}

    assert await handle_cloud_event(cast(Any, repo), {"data": payload}) == {"status": "SUCCESS"}
    assert repo.runs == [] and repo.datasets == []
