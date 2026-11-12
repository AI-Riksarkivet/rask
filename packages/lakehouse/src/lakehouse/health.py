"""Each dependency's latest answer, kept by a background loop of its own, so ``/readyz`` answers from memory (ADR 0203).

A readiness answer read off a dependency on the probe's own request waits for whatever that read waits for, and the
kubelet's probe allows one second. A check queued for a request thread waits behind a burst holding all 40, so every
replica the burst reaches fails readiness together while nothing has failed; lineage's read of its feed through the
catalog took 1.03-1.25 s (measured live 2026-10-19), longer than the probe allows. So each dependency is read here, on
its own interval and within its own budget, and the probe reads the verdicts.

A dependency the service cannot serve without is REQUIRED: while it does not answer, ``/readyz`` answers 503 and
Kubernetes takes the replica out of rotation (owner, 2026-11-12, "Gate on required"). One the service rides through is
reported and never gates: the bus, which the outbox stands in for, and the secret store, whose keys the service already
holds.

A verdict is the latest read's: one that fails or overruns its budget turns it at once, and one no longer made (a loop
starved by a wedged event loop) turns it once the last answer is older than two rounds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from functools import partial
from time import monotonic
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from lakehouse.objectfs import fs_and_base
from lakehouse.schemas.health import Readiness, ReadinessStatus
from lakehouse.supervision import Supervisor


if TYPE_CHECKING:
    from fastapi import FastAPI

    from lakehouse.bus_watchdog import BusWatchdog
    from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: How often every lakehouse process reads each of its dependencies, and how long one read may take: a verdict is at most
#: one round old, and a dependency that stops answering turns its verdict within two (ADR 0203).
HEALTH_INTERVAL_SECONDS: Final = 5.0
HEALTH_BUDGET_SECONDS: Final = 5.0


class Verdict(StrEnum):
    """What a dependency's latest read showed, as `/readyz` names it in its components."""

    healthy = "healthy"
    unavailable = "unavailable"


class Dependency(BaseModel):
    """One thing a service reads to know it can serve: ``read`` raises unless it answers."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    read: Callable[[], Awaitable[object]]
    #: Whether the service cannot serve without it, so that it not answering takes the replica out of rotation.
    required: bool
    #: What releases what ``read`` holds of its own (a client), once the watch on it stops; none for a read on a client
    #: the process holds anyway.
    close: Callable[[], Awaitable[None]] | None = None


def store_dependency(root_uri: str, options: Callable[[], StorageOptions], *, required: bool) -> Dependency:
    """The object store, read as one listing of ``root_uri`` under the options in force at each read (the process's own
    session, renewed as it runs), which a store that is down, or a session it no longer honours, refuses. The blocking
    read runs on the event loop's own executor (``asyncio.to_thread``), never on the request threads Starlette lends a
    sync door, since a burst holding all 40 of those is what the verdict must not wait behind."""
    return Dependency(name="store", read=partial(asyncio.to_thread, _list_the_root, root_uri, options), required=required)


def _list_the_root(root_uri: str, options: Callable[[], StorageOptions]) -> None:
    """One listing of ``root_uri``. Blocking."""
    fs, base = fs_and_base(root_uri, options())
    fs.get_file_info(base)


def openfga_dependency(app: FastAPI, *, required: bool) -> list[Dependency]:
    """OpenFGA, read under the client this process holds (``app.state.fga``), or nothing when it asks OpenFGA nothing."""
    fga = getattr(app.state, "fga", None)
    return [Dependency(name="openfga", read=fga.get_store, required=required)] if fga is not None else []


def bus_dependency(watchdog: BusWatchdog | None) -> list[Dependency]:
    """The bus the chart names this pod, reported and never gating, since every announcement it carries is staged in an
    outbox first and the bus delivers again what a consumer could not take; nothing for a pod named no bus."""
    return [Dependency(name="bus", read=watchdog.bus_answer, required=False)] if watchdog is not None else []


class _Answer(BaseModel):
    """When a dependency last answered, and whether its latest read did (``None`` before the first read)."""

    answered_at: float | None = None
    answering: bool | None = None


class HealthWatch:
    """The latest answer of each of ``dependencies``, read every ``interval`` seconds within ``budget`` seconds each."""

    def __init__(
        self,
        dependencies: Sequence[Dependency],
        *,
        interval: float = HEALTH_INTERVAL_SECONDS,
        budget: float = HEALTH_BUDGET_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        names = [dependency.name for dependency in dependencies]
        if len(set(names)) != len(names):
            raise ValueError(f"each dependency is watched once, got {names}")
        self._dependencies = tuple(dependencies)
        self._interval = interval
        self._budget = budget
        self._clock = clock
        self._answers = {name: _Answer() for name in names}

    def watch(self, loops: Supervisor) -> None:
        """Read each dependency on a loop of its own, named for it, until the process stops."""
        for dependency in self._dependencies:
            loops.keep(f"health-{dependency.name}", partial(self._watch_forever, dependency))

    async def check(self, dependency: Dependency) -> None:
        """Read ``dependency`` once, within the budget, and keep what it showed; a change of verdict is logged."""
        answer = self._answers[dependency.name]
        try:
            async with asyncio.timeout(self._budget):
                await dependency.read()
        except Exception as exc:  # whatever the read raised, the dependency did not answer
            if answer.answering is not False:
                log.warning("dependency_unavailable", extra={"dependency": dependency.name, "error": f"{type(exc).__name__}: {exc}"[:300]})
            answer.answering = False
            return
        if answer.answering is not True:
            log.info("dependency_answering", extra={"dependency": dependency.name})
        answer.answering = True
        answer.answered_at = self._clock()

    def answering(self, name: str) -> bool:
        """Whether the dependency ``name`` answered its latest read, and recently."""
        answer = self._answers[name]
        if not answer.answering or answer.answered_at is None:
            return False
        return self._clock() - answer.answered_at <= 2 * (self._interval + self._budget)

    def readiness(self, facts: Mapping[str, str] | None = None) -> Readiness:
        """The readiness these verdicts give: unhealthy (503) while a required dependency does not answer, degraded (200,
        in rotation) while only one the service rides through does not, else ready; each dependency named in
        ``components`` with its verdict, beside the service's own ``facts``."""
        components = {**(facts or {}), **{d.name: Verdict.healthy if self.answering(d.name) else Verdict.unavailable for d in self._dependencies}}
        down = [d for d in self._dependencies if not self.answering(d.name)]
        if any(d.required for d in down):
            return Readiness(status=ReadinessStatus.unhealthy, components=components)
        return Readiness(status=ReadinessStatus.degraded if down else ReadinessStatus.ready, components=components)

    async def _watch_forever(self, dependency: Dependency) -> None:
        try:
            while True:
                await self.check(dependency)
                await asyncio.sleep(self._interval)
        finally:
            if dependency.close is not None:
                await dependency.close()


__all__ = ["Dependency", "HealthWatch", "Verdict", "bus_dependency", "openfga_dependency"]
