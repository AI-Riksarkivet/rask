"""`_resolve` must reuse the process-wide pooled client, never build one per call.

docs/DECISIONS.md "The Python estate audit" VS-12 — `_resolve` constructed a fresh `httpx.Client(base_url=..., timeout=...)`
on every `/api/page` and `/api/pages` request, paying a new TLS handshake and connection pool per
catalog resolve, while the pooled client built once in the lifespan sits on `state.http` and is
already reused by `system.py`. `state.http` carries no `base_url`, so the resolve passes the catalog
URI as an absolute URL, exactly as the health pings do.
"""

from __future__ import annotations

import pytest

from service_kit.media.config import Settings
from service_kit.media.state import AppState
from viewer.api.v1.endpoints import pages as pages_ep


TABLE = "bronze$pages"
BASE = "http://catalog.internal:2333"
LOCATION = "s3://rask-lake/bronze/pages.lance"


class _SpyResponse:
    status_code = 200

    def json(self) -> dict:
        return {"location": LOCATION}


class _SpyClient:
    """Records every `.post` so the test can prove the pooled client carried the resolve."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs: object) -> _SpyResponse:
        self.calls.append((url, dict(kwargs)))
        return _SpyResponse()


def test_resolve_uses_the_pooled_client_and_builds_no_new_one(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _SpyClient()
    state = AppState(settings=Settings(MEDIA_CATALOG_URI=BASE), http=spy)

    def _no_new_client(**_kwargs: object) -> None:
        raise AssertionError("_resolve built a fresh httpx.Client instead of reusing state.http")

    monkeypatch.setattr(pages_ep.httpx, "Client", _no_new_client)

    location = pages_ep._resolve(state, TABLE, "tok")  # noqa: SLF001

    assert location == LOCATION
    assert len(spy.calls) == 1, "the resolve did not go through the pooled client"
    url, kwargs = spy.calls[0]
    # state.http has no base_url, so the resolve must pass an ABSOLUTE url — like the health pings do.
    assert url == f"{BASE}/v1/table/{TABLE}/describe"
    assert kwargs.get("json") == {}
    assert kwargs.get("headers") == {"Authorization": "Bearer tok"}
