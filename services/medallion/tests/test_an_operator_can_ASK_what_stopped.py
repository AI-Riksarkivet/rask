"""A tier written and then stopped must be answerable without knowing an instance id in advance.

[[LH-167]]. `LagTickReport.unpublished_source` already carries these cells as identities, and
`cascade_lag_cron.py` says they "travel in the report instead, where somebody ASKING gets an answer".
Measured: there was nothing to ask. Every route the producer mounts is keyed by an `instance_id`
(`/promotions/{id}`, `/stages/{id}`, `/trains/{id}`, `/stage-runners/{runner}/stages/{id}`), and the
only API-layer mention of `LagTickReport` is the cron door — `require_dapr_token`-guarded, so the
sidecar is the sole caller AND the sole recipient of the answer.

THE OTHER SURFACE IN THAT ROW IS NOT AVAILABLE, which is why this one is built. Enumerating held
promotions would mean listing Dapr Workflow instances, and `DaprWorkflowClient` exposes no list or
query method at all — `get_workflow_state`, `pause`, `purge`, `raise_event`, `resume`, `schedule`,
`terminate`, `wait_for_*`, every one keyed by an instance id. That surface needs a side index built
first; this one needs a route.

STILL NOT A METRIC SERIES, and the door does not make it one. The detector cannot tell a tier that
stopped from a lane created five minutes ago, so a series would fire on every fresh lane — the
objection `test_a_source_that_exists_but_never_published_is_not_reported` makes and which still holds.
A pull-shaped answer has no such problem: nobody is paged, and somebody asking gets the identities.

THE READ MUST NOT MOVE THE SERIES EITHER. `run_lag_tick` requires a gauge, and passing the real one
would publish lag points every time a human opened the page — a dashboard that jumps when it is looked
at. The door passes a no-op gauge and no memo, so a read changes no detector state.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence

import httpx
import pytest
from fastapi import FastAPI

from medallion.services.cascade_lag import AbsentEdgeMemo, ConsumedReader, LagGauge, LagTickReport, PublishedReader, StalledTier


def _report(*, stalled: list[tuple[str, str]]) -> LagTickReport:
    """A tick that measured `edges` cells and found `stalled` of them written-but-unpublished.

    `edges`, `published_points` and `failed` are REQUIRED on the model — the denominator every other
    field is read against — so they are supplied rather than defaulted: a report whose denominator is
    invented would let the projection under test pass over a shape the tick never produces.
    """
    return LagTickReport(
        edges=len(stalled),
        published_points=0,
        failed=0,
        unpublished_source=[StalledTier(edge=edge, project=project) for edge, project in stalled],
    )


def test_the_answer_carries_only_the_cells_of_VISIBLE_projects() -> None:
    """Which projects are visible is the door's decision (`test_the_operator_doors_authorize_on_the_resource`);
    the projection keeps exactly those cells and nothing else."""
    from medallion.api.cascade_lag_read import stalled_from

    answered = stalled_from(_report(stalled=[("silver->gold", "acme"), ("bronze->silver", "other")]), visible=frozenset({"other"}))

    assert [(cell.edge, cell.project) for cell in answered.unpublished_source] == [("bronze->silver", "other")]


def test_the_door_measures_OFF_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declaration lists the warehouse registry and the tick reads the catalog and lineage per
    edge, all synchronously; the cron door's own measurement is a 15 s tick that timed the pod's probes
    out (`test_the_lag_tick_does_not_block_the_event_loop.py`, which also says why the assertion is the
    thread and not a probe's latency). `/api/cascade` publishes this door at the edge."""
    from medallion.api import cascade_lag_read
    from medallion.api.produce_auth import ProducerCaller, admit_caller

    ran_on: dict[str, int] = {}

    def declared(_settings: object) -> list[tuple[str, str]]:
        ran_on["declared_edges"] = threading.get_ident()
        return [("silver->gold", "acme")]

    def tick(
        *, edges: Sequence[tuple[str, str]], published: PublishedReader, consumed: ConsumedReader, gauge: LagGauge, memo: AbsentEdgeMemo | None = None
    ) -> LagTickReport:
        ran_on["run_lag_tick"] = threading.get_ident()
        return _report(stalled=[("silver->gold", "acme")])

    monkeypatch.setattr("medallion.services.cascade_lag_readers.declared_edges", declared)
    monkeypatch.setattr(cascade_lag_read, "run_lag_tick", tick)
    app = FastAPI()
    app.include_router(cascade_lag_read.router)
    app.dependency_overrides[admit_caller] = ProducerCaller

    async def ask() -> tuple[int, httpx.Response]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            return threading.get_ident(), await client.get("/cascade/stalled")

    loop_thread, response = asyncio.run(ask())

    assert response.status_code == 200, response.text
    assert set(ran_on) == {"declared_edges", "run_lag_tick"}, ran_on
    assert loop_thread not in ran_on.values(), f"blocking work ran on the event loop's thread: {ran_on}"
