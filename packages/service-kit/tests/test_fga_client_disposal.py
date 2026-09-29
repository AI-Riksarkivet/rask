"""An aiohttp-backed OpenFGA client must be closed by whoever opened it.

open_fastapi-audit — "Four lifespans build an aiohttp-backed OpenFGA client and never close it — the
estate has the exact fix written down at a fifth site".

`production-patterns.md`: "Always clean up after `yield`. Pools leak on shutdown otherwise." The SDK's
session is aiohttp, so an unclosed client leaves one half-open connection per replica on OpenFGA until
its own idle timeout, and the only trace is an "Unclosed client session" line on the way out.

THE WEIGHT IS SMALL AND THE SHAPE IS THE POINT. A drain window's half-open connection is reclaimed by
the pod's own exit; that is why the audit regrades this low. What makes it worth fixing is that FIVE
lifespans call one factory and only one of them disposed — so the fix is not five copies of a block,
it is one disposer the factory's own package owns. The audit says exactly that: "make it
un-forgettable rather than per-service".

Suppressed rather than raised, for the reason the notifications block already gives: a shutdown path
that raises hides whatever came after it, and a failed close cannot be retried on a pod that is
leaving.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_a_failing_close_does_not_stop_the_teardown() -> None:
    """ "A shutdown path that raises hides whatever came after it" — the notifications block's own words."""
    from fastapi import FastAPI

    from service_kit.governed.fga import dispose

    class _Angry:
        async def close(self) -> None:
            raise RuntimeError("connection already gone")

    app = FastAPI()
    app.state.fga = _Angry()
    await dispose(app)  # must not raise
