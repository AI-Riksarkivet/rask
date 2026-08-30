"""A durable worker's Ray client must have the worker's lifetime, not the call's.

open_fastapi-audit — "The Ray dashboard client is rebuilt per durable-workflow activity while the
catalog client beside it is pooled, in the module family whose own comment cites the rule".

Every Ray submit opened `httpx.AsyncClient(...)` in an `async with` and tore it down on the way out —
one TCP connect, one TLS handshake and one pool teardown per submit, on a durable workflow that runs
these activities repeatedly. `production-patterns.md`: "One engine, one HTTP client, per process.
Created in lifespan, stashed on `app.state`."

THE HONEST CAVEAT, which the finding makes and this gate keeps: a workflow ACTIVITY has no `Request`
and no reachable `app.state`, so the reference's literal prescription does not apply. That is why the
answer here is a module-level client with the WORKER's lifetime, closed when the runtime shuts down —
not a lifespan-owned one it has no way to reach.

AND THE CLOSE MATTERS AS MUCH AS THE POOL. A module-level client that nothing closes trades a
per-call teardown for a permanent leak plus a "Unclosed client session" on every shutdown. The test
below pins both halves, because the pooling half alone is the easier and worse fix.

`mover.py` already pools its catalog client, so this was MED-008 partially applied — the workflow
activities were its unfixed remainder.
"""

from __future__ import annotations

import inspect

import pytest

from medallion.services import ray_submit


def test_the_module_offers_one_pooled_client() -> None:
    assert hasattr(ray_submit, "ray_client"), (
        "ray_submit builds a fresh httpx.AsyncClient per submit — one connect, handshake and pool "
        "teardown per activity on a durable workflow that runs them repeatedly"
    )


def test_no_submit_path_builds_its_own_client() -> None:
    """The accessor existing is not the property; the call sites using it is."""
    for name in ("submit_stage_job", "submit_train_job"):
        fn = getattr(ray_submit, name, None)
        if fn is None:
            continue
        source = inspect.getsource(fn)
        code = "\n".join(line for line in source.split("\n") if not line.strip().startswith("#"))
        assert "AsyncClient(" not in code, f"{name} still constructs its own client"


@pytest.mark.asyncio
async def test_the_client_is_reused_across_calls() -> None:
    first = await ray_submit.ray_client()
    second = await ray_submit.ray_client()
    assert first is second, "each call got a new client — the pool is per-call again"
    await ray_submit.close_ray_client()


@pytest.mark.asyncio
async def test_it_can_be_closed_and_rebuilt() -> None:
    """A module-level client nothing closes is a leak plus an 'Unclosed client session' on every
    shutdown — the easier half of this fix, and the worse one on its own."""
    first = await ray_submit.ray_client()
    await ray_submit.close_ray_client()
    assert first.is_closed

    second = await ray_submit.ray_client()
    assert second is not first, "after close, the next caller must get a working client"
    await ray_submit.close_ray_client()
