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

from functools import partial

from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from medallion.api.dependencies import FgaClientDep, SettingsDep
from medallion.api.produce_auth import AdmittedCaller, administered_projects
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


def stalled_from(report: LagTickReport, *, visible: frozenset[str]) -> StalledTiers:
    """Project one tick's report onto the answer this door gives: the cells of ``visible`` projects.

    Separate from the route so the projection is testable without standing up the readers, and so the
    route stays the thin half: parse, measure, authorize, project.
    """
    return StalledTiers(unpublished_source=[cell for cell in report.unpublished_source if cell.project in visible])


@router.get("/cascade/stalled")
async def stalled_tiers(settings: SettingsDep, fga_client: FgaClientDep, caller: AdmittedCaller) -> StalledTiers:
    """Which tiers were written and never published — answerable without knowing an instance id.

    Each cell names its project, and a caller sees the cells of the projects they administer
    (`can_administer`, one `batch_check` over the declared projects): a stalled tier is a tenant's
    pipeline state, and an admin of one tenant has no claim on another's. Administering none is an
    empty answer, not a 403 — the same rule as ingest's cross-tenant listing.

    AUTHORIZED BEFORE IT IS MEASURED. The tick is a catalog and a lineage read per edge under this
    service's credentials, so only the caller's own edges are measured, and a caller holding no grant
    costs one registry listing. Both run in the threadpool: they are synchronous, and the cron door's
    tick held the event loop for 15 s when run inline (`cascade_lag_cron.py`).

    MEASURED PER REQUEST, not served from the cron's last tick. A cached answer would need somewhere to
    live and would go stale exactly when it matters — the question is asked BECAUSE something looks
    wrong, and the useful answer is about now.
    """
    from medallion.services.cascade_lag_readers import consumed_reader, declared_edges, published_reader

    edges = await run_in_threadpool(declared_edges, settings)

    # The single-tenant row is project `""`: the unqualified lanes `/produce` writes with no `?project=`,
    # gated there on the configured project, so it is authorized on that project here.
    def tenant(project: str) -> str:
        return project or settings.produce_admin_project

    visible = await administered_projects(fga_client, caller, (tenant(project) for _edge, project in edges))
    own = [(edge, project) for edge, project in edges if tenant(project) in visible]
    if not own:
        return StalledTiers()
    report = await run_in_threadpool(
        partial(run_lag_tick, edges=own, published=published_reader(settings), consumed=consumed_reader(settings), gauge=_silent_gauge())
    )
    return stalled_from(report, visible=frozenset(project for _edge, project in own))
