"""Six apps buffered request bodies with no ceiling; the cap existed in one service only.

`BodySizeLimitMiddleware` was written for the catalog's Arrow-IPC write endpoints, which read the
whole body into memory before the handler runs — a single unbounded POST OOMs the worker. It lived in
`services/catalog/src/catalog/api/body_limit.py` and applied to the catalog alone.

Every other app got CORS + RequestID + Timing and nothing else. That includes the GATEWAY, which
buffers every proxied request body whole before forwarding it, and is the one app published at the
edge — so the estate's least-protected body path was also its most exposed.

Owner ruling 2026-08-26: body caps land as ONE SEAM in `service_kit.middleware.register_middleware`,
so a service cannot be added without one and a new route cannot arrive uncapped.

THE CAP IS A DoS CEILING, NOT A BUSINESS RULE. It is deliberately generous: it exists to stop a
multi-GB body reaching a buffer, not to express what a payload should be. A route that wants a
tighter bound states it itself — the catalog keeps its own 256 MiB for Arrow-IPC, which is larger
than the shared default and set explicitly for that reason.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from service_kit import make_service_app
from service_kit.config import Settings


def test_an_oversized_body_is_REFUSED_with_413() -> None:
    """End to end through a real app built by the factory."""
    app = make_service_app(title="capped", routers=[])

    @app.post("/echo")
    async def _echo() -> dict[str, str]:
        return {"ok": "yes"}

    cap = Settings().max_body_bytes
    with TestClient(app) as client:
        resp = client.post("/echo", content=b"x" * (cap + 1))

    assert resp.status_code == 413, f"an over-cap body answered {resp.status_code}, not 413"


def test_a_normal_body_still_passes() -> None:
    """The guard that stops the ceiling being tightened into a business rule."""
    app = make_service_app(title="capped2", routers=[])

    @app.post("/echo")
    async def _echo() -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as client:
        resp = client.post("/echo", content=b"x" * 1024)

    assert resp.status_code == 200
