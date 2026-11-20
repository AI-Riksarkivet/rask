"""The catalog's fixed timers: each tick a loop of this process, each window claimed on the control root.

Every replica runs every loop, and `lakehouse.timers` lets one replica's claim of a window land, so each tick runs once
a window whatever the replica count. A timer runs when git gives it a period above zero. Through a read-only window
every timer is held: a tick writes (a release deletes tags and branches, a reclaim deletes versions), so it claims
nothing and runs nothing until the window ends, and the first window after it does the work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from catalog.api import commit_reconcile, control_relay, declaration_converge, erasure_release, manifest_reclaim
from catalog.api.ticks import Tick
from catalog.core.config import Settings
from lakehouse.events import outbox_metrics
from lakehouse.supervision import Supervisor
from lakehouse.timers import Timer


type TickWork = Callable[[Tick], Awaitable[BaseModel]]

#: Where the app keeps the timers it runs, by name.
TIMERS_STATE = "timers"


def _nothing() -> None:
    return None


class CatalogTimer(BaseModel):
    """One catalog timer: the period git gives it, zero for one that is off, and the tick it runs."""

    model_config = ConfigDict(frozen=True)

    period: float
    tick: TickWork
    #: What a replica drops on a window another replica ran (`lakehouse.timers.Timer`).
    elsewhere: Callable[[], None] = _nothing


def catalog_timers(settings: Settings) -> dict[str, CatalogTimer]:
    """Each catalog timer by name."""
    return {
        # The relay measures the control outbox's backlog on each tick, so a replica another ran the window of drops its own.
        "catalog-control-relay": CatalogTimer(
            period=settings.control_relay_period_seconds, tick=control_relay.tick, elsewhere=partial(outbox_metrics.forget_backlog, "control")
        ),
        "catalog-erasure-release": CatalogTimer(period=settings.erasure_release_period_seconds, tick=erasure_release.tick),
        "catalog-commit-reconcile": CatalogTimer(period=settings.commit_reconcile_period_seconds, tick=commit_reconcile.tick),
        "catalog-manifest-reclaim": CatalogTimer(period=settings.manifest_reclaim_period_seconds, tick=manifest_reclaim.tick),
        "catalog-declaration-converge": CatalogTimer(period=settings.declaration_converge_period_seconds, tick=declaration_converge.tick),
    }


def start_timers(app: FastAPI, settings: Settings, loops: Supervisor) -> None:
    """Keep a loop of ``loops`` for every timer git gives a period, and publish each on ``app.state``.

    Called once everything a tick works with is on ``app.state``; the loops are stopped with the process's others,
    before any of it is released.
    """
    outbox_metrics.create_series("control")
    commit_reconcile.create_series()
    manifest_reclaim.create_series()
    timers: dict[str, Timer] = {}
    for name, timer in catalog_timers(settings).items():
        if not timer.period:
            continue
        timers[name] = Timer(
            name,
            period=timer.period,
            work=partial(_run, app, settings, timer.tick),
            root=settings.registry_root,
            storage_options=settings.storage_options,
            held=lambda: settings.maintenance_read_only,
            elsewhere=timer.elsewhere,
        )
        loops.keep(f"timer-{name}", timers[name].run)
    setattr(app.state, TIMERS_STATE, timers)


async def _run(app: FastAPI, settings: Settings, tick: TickWork) -> None:
    await tick(Tick(app, settings))
