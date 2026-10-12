"""Name -> adapter: the seam that turns a chosen engine into the thing that runs it (CP-056).

Two kinds of engine and no third. The in-process reference engine is this plane's own; every other engine is a key
of the deployment's `MEDALLION_ENGINES`, reached through `service_kit.lakehouse.engine_door.InvokedExecutor`, which
names no engine. So a new engine is a new deployment entry and a new app, never an edit here, and the lakehouse
imports no engine adapter (the `the-lakehouse-knows-no-engine` import contract).

ONE POOLED HTTP CLIENT PER PROCESS for every invoked engine (`production-patterns.md`: one client per process). It is
built on first use, on the planner's own loop, and closed by `close_executors` from each planner's shutdown.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import httpx

from medallion.services.engine_names import IN_PROCESS_ENGINE
from service_kit.lakehouse.engine_door import InvokedExecutor


if TYPE_CHECKING:
    from medallion.core.config import MedallionSettings
    from service_kit.lakehouse.executor import Executor


class UnknownEngineError(RuntimeError):
    """A declared engine this deployment hosts no adapter for.

    RAISED, never defaulted: falling back to whichever engine happens to be configured is how a declaration meant for
    another plane gets silently run here, which `engine_choice` already refuses at declaration time.
    """


_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _engine_client(timeout_seconds: float) -> httpx.AsyncClient:
    """The process's one client to the engine apps, rebuilt only if its event loop is not the running one.

    A client's pooled connections belong to the loop that opened them, so one carried onto another loop fails with
    "Event loop is closed"; a service runs one loop, so this builds once. No await between the check and the set.
    """
    global _client, _client_loop
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=timeout_seconds)
        _client_loop = loop
    return _client


def hosted_engines(settings: MedallionSettings) -> frozenset[str]:
    """The engines THIS DEPLOYMENT runs: the in-process one, and every engine its configuration names."""
    return frozenset({IN_PROCESS_ENGINE, *settings.engines})


def executor_for(settings: MedallionSettings, engine: str) -> Executor:
    """The adapter that runs ``engine``, or `UnknownEngineError`."""
    if engine == IN_PROCESS_ENGINE:
        from medallion.services.inprocess_executor import InProcessExecutor

        return InProcessExecutor(settings.storage_options)
    binding = settings.engines.get(engine)
    if binding is None:
        raise UnknownEngineError(
            f"no executor is registered for engine {engine!r}; this deployment hosts {sorted(hosted_engines(settings))}. "
            "A task registered for another engine belongs to another deployment and is refused rather than run here."
        )
    return InvokedExecutor(engine, binding, _engine_client(settings.engine_request_timeout_seconds), dapr_http_port=settings.dapr_http_port)


async def close_executors() -> None:
    """Release the pooled engine client; a planner's lifespan calls it once, at shutdown. Idempotent and tolerant."""
    global _client, _client_loop
    if _client is not None:
        with suppress(Exception):
            await _client.aclose()
    _client = None
    _client_loop = None
