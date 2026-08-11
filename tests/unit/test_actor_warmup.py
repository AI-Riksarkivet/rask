"""The actor-proxy warm-up: it must move the SDK's blocking handshake OFF the request path.

The defect this closes was measured against a real uvicorn process, not reasoned about. Dapr's
`ActorProxy.create` builds a process-global factory on first use, and that constructor calls
`DaprHealth.wait_for_sidecar()` — a synchronous `urllib` + `time.sleep` loop, 60 s by default. So the
first actor call in a process pays it, ON THE EVENT LOOP: one cron tick against an unreachable
sidecar pinned the notifications service for 60 s, and the reconcile pass's own `asyncio.timeout`
never fired, because the loop the timer would fire on was the blocked one.

Two properties are worth a test and neither is about Dapr: the warm-up must not RAISE (a lifespan
that raises takes the pod down over a capability the service can refuse), and it must not BLOCK the
loop (which is the whole point).
"""

import asyncio
import time

import pytest

from service_kit.governed import actor_warmup


@pytest.mark.asyncio
async def test_a_blocking_handshake_does_not_stall_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing one. A sleeping factory constructor must not stop other coroutines running."""

    def _slow_factory() -> None:
        time.sleep(0.30)  # stands in for wait_for_sidecar's sleep loop

    monkeypatch.setattr(actor_warmup, "_build_factory", _slow_factory)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks += 1

    # ELAPSED, not a tick count. Counting ticks would pass either way: a blocking warm-up simply runs
    # first and the ticker completes afterwards, still reaching 20. Only the wall clock separates
    # "concurrent" from "serialized" — max(0.30, 0.20) vs 0.30 + 0.20.
    started = time.perf_counter()
    await asyncio.gather(actor_warmup.warm_actor_proxy_factory(timeout_seconds=5.0), _ticker())
    elapsed = time.perf_counter() - started

    assert ticks == 20
    assert elapsed < 0.45, f"warm-up serialized with the ticker ({elapsed:.2f}s) — it ran ON the event loop"


@pytest.mark.asyncio
async def test_a_sidecar_that_never_answers_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lifespan calls this. It must degrade one capability, never the pod."""

    def _never() -> None:
        time.sleep(5)

    monkeypatch.setattr(actor_warmup, "_build_factory", _never)

    assert await actor_warmup.warm_actor_proxy_factory(timeout_seconds=0.05) is False


@pytest.mark.asyncio
async def test_a_failing_constructor_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise RuntimeError("no sidecar")

    monkeypatch.setattr(actor_warmup, "_build_factory", _boom)

    assert await actor_warmup.warm_actor_proxy_factory() is False


@pytest.mark.asyncio
async def test_a_reachable_sidecar_warms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actor_warmup, "_build_factory", lambda: None)

    assert await actor_warmup.warm_actor_proxy_factory() is True
