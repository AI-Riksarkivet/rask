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

from medallion.services.cascade_lag import LagTickReport, StalledTier


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


def test_the_door_answers_what_stopped_without_an_instance_id() -> None:
    """THE DEFECT: the identities existed and no caller could reach them."""
    from medallion.api.cascade_lag_read import stalled_from

    answered = stalled_from(_report(stalled=[("silver->gold", "acme"), ("bronze->silver", "brand-new")]))

    assert [(cell.edge, cell.project) for cell in answered.unpublished_source] == [
        ("silver->gold", "acme"),
        ("bronze->silver", "brand-new"),
    ]


def test_a_cascade_with_nothing_stopped_answers_an_empty_list() -> None:
    """ "Nothing is stalled" and "the door is broken" must not look alike, so it answers rather than 404s."""
    from medallion.api.cascade_lag_read import stalled_from

    assert stalled_from(_report(stalled=[])).unpublished_source == []


def test_the_gauge_the_door_hands_the_tick_RECORDS_NOTHING() -> None:
    """A dashboard that moves because somebody looked at it is worse than one that is stale.

    Asserted by driving the gauge through `record_edge_lag` — the production path that decides when a
    point is published — rather than by calling `.set` and checking a list nothing appends to. An
    earlier draft did exactly that, and with its spy removed the assertion was `[] == []`: green
    against any gauge, including the real one.
    """
    from medallion.api.cascade_lag_read import _silent_gauge
    from medallion.services.cascade_lag import EdgeLag, record_edge_lag

    real_points: list[int] = []

    class _Recording:
        def set(self, value: int, /, attributes: dict[str, str] | None = None) -> None:
            real_points.append(value)

    measurable = EdgeLag(edge="silver->gold", project="acme", lag=3, known=True)
    record_edge_lag(measurable, gauge=_Recording())
    assert real_points == [3], "the fixture does not describe a lag this path would publish, so the control below proves nothing"

    record_edge_lag(measurable, gauge=_silent_gauge())

    assert real_points == [3], "the read door's gauge published a point"
