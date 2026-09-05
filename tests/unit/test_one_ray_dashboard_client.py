"""One Ray-dashboard HTTP client per worker (docs/DECISIONS.md "The Python estate audit" DUP-21).

DUP-21 filed twelve outbound call sites that built a fresh `httpx` client per call. Most have since
been pooled — `ray_submit` grew `ray_client()`, `viewer/api/v1/endpoints/pages.py` grew a
process-wide pool, `ingest/http.py` and `flows`/`notifications`/`compute` hold app-scoped clients —
but `medallion/workflow.py` kept building its own, THREE times, against the same Ray dashboard the
pool in `ray_submit` was created for:

    async with httpx.AsyncClient(base_url=settings.ray_address, timeout=…) as client:

Those three run inside durable workflow ACTIVITIES: `poll_stage_job` and `poll_train_job` are called
on every polling tick of every running stage, so the cost is one TCP connect, one TLS handshake and
one pool teardown per tick, on the hottest path the cascade has — and it is paid beside a pooled
client for the same host that the mover's lifespan already opens and closes.

`ray_submit.ray_client()` is that pool, and it is not merely "build once": it is keyed on the
configured address, so a repointed `MEDALLION_RAY_ADDRESS` rebuilds rather than silently answering
from a client bound to the old one. A per-call client got that right by accident; the fix must keep
it, which is why the collapse is onto that function rather than onto a plain module global.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_no_medallion_activity_builds_its_own_ray_client() -> None:
    """RED before the collapse: `medallion/workflow.py` matched three times.

    Scoped to `services/medallion` because that is where the pool lives; the other planes hold their
    clients on `app.state` and are not the drift this finding names.
    """
    per_call = re.compile(r"httpx\.AsyncClient\(base_url=settings\.ray_address")
    offenders = sorted(
        f"{path.relative_to(REPO)}:{i}"
        for path in (REPO / "services/medallion/src").rglob("*.py")
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if per_call.search(line)
    )
    assert offenders == [], offenders


def test_the_pooled_client_is_rebuilt_when_the_ray_address_moves() -> None:
    """The property a per-call client had for free, which the pool must not lose.

    `AsyncClient` binds `base_url` at construction, so a plain build-once cache would go on answering
    with a client aimed at whatever address was configured the first time an activity ran.
    """
    import asyncio

    from medallion.services import ray_submit

    async def _drive() -> tuple[str, str]:
        await ray_submit.close_ray_client()
        ray_submit.get_settings.cache_clear()
        first = await ray_submit.ray_client()
        first_address = str(first.base_url)
        import os

        os.environ["MEDALLION_RAY_ADDRESS"] = "http://ray-moved:8265"
        ray_submit.get_settings.cache_clear()
        try:
            second = await ray_submit.ray_client()
            return first_address, str(second.base_url)
        finally:
            os.environ.pop("MEDALLION_RAY_ADDRESS", None)
            ray_submit.get_settings.cache_clear()
            await ray_submit.close_ray_client()

    before, after = asyncio.run(_drive())
    assert before != after, (before, after)
    assert after.startswith("http://ray-moved:8265"), after
