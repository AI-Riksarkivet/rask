"""Two producer-side contracts the plane cannot repair downstream, and neither had a test.

`.claude/skills/rask-notifications` states the shape: "a state change that names nobody is not
under-delivered, it is UNDELIVERABLE", and `notifiable()` answers an event it cannot target with a
SUCCESS ack — so a producer that drops a field fails silently and is reported by nothing.

1. TRAP 3 — `lance.project`. `fanout.py:88` skips the watcher loop entirely when `project` is None,
   and the run is still delivered to its AUTHOR, so the event looks completely healthy and simply
   reaches fewer people. Measured 2026-08-22: deleting the stage runner's stamp left 4,853 tests passing.
2. The `/produce` 503 tail. The route's own docstring makes it load-bearing — the bronze-write emit is
   the cascade head, so "a publish failure surfaces as 503 (not the 202 that would hide it)". Line
   coverage reported the whole branch missing: the `publish_failed` check, the problem+json body and
   the `Retry-After` header never executed.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from dapr.aio.clients import DaprClient

from medallion.api import produce as produce_route
from medallion.core.config import MedallionSettings


#: Stands in for a resolved dependency the code under test never touches.
_UNUSED = object()


@pytest.mark.asyncio
async def test_a_failed_publish_answers_503_and_not_the_202_that_would_hide_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cascade head's failure must be visible to the caller, not swallowed into a 202."""

    async def _publish_failed(*_a: object, **_k: object) -> dict[str, str]:
        return {"status": "publish_failed", "token": "tok"}

    monkeypatch.setattr(produce_route, "run_produce", _publish_failed)
    # Neither dependency is touched on this path — `run_produce` is patched — so a cast is the
    # honest way to satisfy the resolved-dependency signature without loosening it.
    response: Any = await produce_route.produce(
        dapr=cast(DaprClient, _UNUSED), settings=cast(MedallionSettings, _UNUSED), idempotency_key="idem-test", originator=None
    )

    assert response.status_code == 503, "a dropped cascade head must not answer 202 — the run silently never happens"
    assert response.headers["Retry-After"] == "5"
    assert response.media_type == "application/problem+json"
