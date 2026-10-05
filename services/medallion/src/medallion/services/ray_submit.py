"""The Ray adapter's one pooled dashboard client, for this worker process.

Part of the Ray ADAPTER (`rayjobs_api_executor`), and nothing outside it imports this module: the
`only-the-ray-adapter-speaks-the-jobs-api` import contract refuses it to every other medallion module. Stages and
training both reach Ray through `service_kit.lakehouse.executor` (CP-044), and the planners close this client through
`engine_registry.close_executors` at shutdown.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import httpx

from medallion.core.config import get_settings


#: The ONE Ray dashboard client for this worker process. `production-patterns.md`: "One engine, one HTTP client,
#: per process" — a client per submit pays a TCP connect and a pool teardown on every call.
#:
#: MODULE-LEVEL rather than lifespan-owned because the adapter is resolved BY NAME from the registry
#: (`engine_registry.executor_for`) on every path that reads or drives a run — dispatch, the plan sweep, the operator
#: routes — and none of them hands it `app.state`. The client gets the WORKER's lifetime instead, and
#: `close_ray_client()` is called from each planner's shutdown (`engine_registry.close_executors`): a module-level
#: client nothing closes trades a per-call teardown for a permanent leak plus an "Unclosed client session" on every
#: stop.
#:
#: Guarded by a lock: two callers starting concurrently would otherwise both see `None` and build two clients, one of
#: which is then leaked with no reference to close it.
_client: httpx.AsyncClient | None = None
_client_address: str | None = None
_client_lock = asyncio.Lock()


async def ray_client() -> httpx.AsyncClient:
    """The pooled client, built on first use and rebuilt if the Ray address changes.

    KEYED ON THE ADDRESS, not merely cached. An `AsyncClient` binds `base_url` at construction, so a
    plain "build once" cache would keep answering with a client pointed at whatever address happened to
    be configured the FIRST time a caller asked — silently, and long after the setting changed. That is
    free in production, where the address is stable, and it is the difference between a cache and a
    stale global.
    """
    global _client, _client_address
    settings = get_settings()
    address = settings.ray_address
    if _client is not None and not getattr(_client, "is_closed", False) and _client_address == address:
        return _client
    async with _client_lock:
        if _client is None or getattr(_client, "is_closed", False) or _client_address != address:
            if _client is not None:
                with suppress(Exception):
                    await _client.aclose()
            _client = httpx.AsyncClient(base_url=address, timeout=settings.ray_request_timeout_seconds)
            _client_address = address
    return _client


async def close_ray_client() -> None:
    """Close the pooled client. Idempotent, so a double shutdown is not an error."""
    global _client, _client_address
    # TOLERANT ON PURPOSE, for the same reason the stage runner's teardown suppresses: a shutdown that raises
    # on an already-broken (or substituted) client must not stop the rest of the teardown. The refs are
    # dropped either way, so a failed close cannot leave a stale client answering later callers.
    if _client is not None:
        with suppress(Exception):
            await _client.aclose()
    _client = None
    _client_address = None
