"""The pull-shaped answer to "what stopped?" — [[LH-167]].

`LagTickReport.unpublished_source` carries one cell per tier whose SOURCE exists, has never published,
and whose destination therefore cannot be measured: a table that was created and written and then went
no further. `cascade_lag_cron.py` says those identities "travel in the report instead, where somebody
ASKING gets an answer" — and until this module there was nothing to ask. Every route the producer
mounted was keyed by an `instance_id`, and the only API-layer use of `LagTickReport` was the cron door,
guarded by `require_dapr_token`, so the sidecar was both the sole caller and the sole recipient.

**A PULL, NOT A SERIES, and the distinction is the whole reason this is a route.** The detector cannot
tell a tier that stopped from a lane created five minutes ago, so a gauge would fire on every fresh
lane — the objection `test_a_source_that_exists_but_never_published_is_not_reported` makes and which
still holds. Pulling has no such problem: nobody is paged for a new lane, and somebody asking gets the
identities. That decision is unchanged here; this module only makes it reachable.

**THE ENUMERATION THE ROW ALSO OFFERS IS NOT AVAILABLE.** Listing HELD promotions would mean listing
Dapr Workflow instances, and `DaprWorkflowClient` exposes no list or query method — `get_workflow_state`,
`pause_workflow`, `purge_workflow`, `raise_workflow_event`, `resume_workflow`, `schedule_new_workflow`,
`terminate_workflow`, `wait_for_*`, each keyed by an instance id (measured on the installed SDK). That
surface needs a side index recorded at hold time; this one needed a route.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from medallion.api.dependencies import SettingsDep
from medallion.api.produce_auth import authorize_produce
from medallion.services.cascade_lag import LagGauge, LagTickReport, StalledTier, run_lag_tick


router = APIRouter()


class StalledTiers(BaseModel):
    """Tiers written and then stopped, as identities an operator can act on."""

    unpublished_source: list[StalledTier] = Field(default_factory=list)


class _SilentGauge:
    """A gauge that records nothing, because a READ must not move a published series.

    `run_lag_tick` requires a gauge and publishes a point per measurable edge. Handing it the real one
    here would move the lag series every time a human opened the page — a dashboard that jumps because
    it was looked at, and a lag history that records reads as events. The tick's arithmetic is wanted;
    its publication is not.
    """

    #: The protocol's own shape, copied exactly — `value` positional-only, `attributes` by name. A
    #: stand-in whose signature merely resembles the one it replaces is the shape `LagGauge`'s own
    #: comment warns about: satisfied by the double and by nothing in production.
    def set(self, value: int, /, attributes: dict[str, str] | None = None) -> None:
        return None


def _silent_gauge() -> LagGauge:
    return _SilentGauge()


def stalled_from(report: LagTickReport) -> StalledTiers:
    """Project one tick's report onto the answer this door gives.

    Separate from the route so the projection is testable without standing up the readers, and so the
    route stays the thin half: parse, authorize, measure, project.
    """
    return StalledTiers(unpublished_source=list(report.unpublished_source))


@router.get("/cascade/stalled")
async def stalled_tiers(settings: SettingsDep, _subject: Annotated[str | None, Depends(authorize_produce)]) -> StalledTiers:
    """Which tiers were written and never published — answerable without knowing an instance id.

    Gated like its `/stage-runners` sibling rather than more loosely: the cells name projects and
    edges, which is estate shape, and a reader who may not produce has no claim on it.

    MEASURED PER REQUEST, not served from the cron's last tick. A cached answer would need somewhere to
    live and would go stale exactly when it matters — the question is asked BECAUSE something looks
    wrong, and the useful answer is about now. The cost is bounded by the declared edge count, which is
    the same bound the cron pays on its own schedule.
    """
    from medallion.services.cascade_lag_readers import consumed_reader, declared_edges, published_reader

    report = run_lag_tick(
        edges=declared_edges(settings),
        published=published_reader(settings),
        consumed=consumed_reader(settings),
        gauge=_silent_gauge(),
    )
    return stalled_from(report)
