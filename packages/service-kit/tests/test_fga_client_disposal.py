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

import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parents[3]

#: Every lifespan that builds an FGA client. All of them call one factory; only notifications closed it.
LIFESPANS = [
    "services/annotator/src/annotator/main.py",
    "services/viewer/src/viewer/main.py",
    "services/flows/src/flows/lifespan.py",
    "services/maintenance/src/maintenance/service.py",
    "services/notifications/src/notifications/lifespan.py",
]


@pytest.mark.asyncio
async def test_the_disposer_closes_a_client_on_app_state() -> None:
    from fastapi import FastAPI

    from service_kit.governed.fga import dispose

    closed: list[bool] = []

    class _Client:
        async def close(self) -> None:
            closed.append(True)

    app = FastAPI()
    app.state.fga = _Client()
    await dispose(app)
    assert closed == [True]


@pytest.mark.asyncio
async def test_the_disposer_is_silent_when_there_is_nothing_to_close() -> None:
    """FGA off, or a lifespan that failed before building one — neither is an error at shutdown."""
    from fastapi import FastAPI

    from service_kit.governed.fga import dispose

    await dispose(FastAPI())  # no attribute at all
    app = FastAPI()
    app.state.fga = None
    await dispose(app)


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


@pytest.mark.parametrize("path", LIFESPANS, ids=[p.split("/")[1] for p in LIFESPANS])
def test_every_lifespan_disposes_its_client(path: str) -> None:
    """Five lifespans, one factory, one disposer — not five copies of a block that four forgot."""
    source = (REPO / path).read_text()
    assert "dispose" in source, (
        f"{path} builds an FGA client and never closes it — the SDK's aiohttp session is collected "
        f"unclosed, leaving a half-open connection per replica on every rolling restart"
    )
