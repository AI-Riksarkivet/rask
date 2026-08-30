"""ING-08 — the plane opens ONE connection pool, not one per outbound call.

`fetch.py`'s own module docstring already argues that the HTTP fetcher must own a generic client,
and then built a fresh `httpx.Client` inside `_fetch_http` — which is called ONCE PER UNIT. A
million-unit run is a million TCP+TLS handshakes against the source, and the ceiling that exists to
overlap fetches (`fetch_concurrency`) buys nothing when each one starts by connecting.

The catalog client and the provenance read had the same shape through the module-level
`httpx.post` / `httpx.get` helpers, which build and discard a client internally on every call.

These assert the POOL — that two calls reuse one client — because the responses are identical either
way, so a test that asserts only on the answer passes against the defect.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest


def test_the_shared_client_is_one_object(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest.http import shared_client

    shared_client.cache_clear()
    assert shared_client() is shared_client()


def test_two_http_unit_fetches_share_one_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """One handshake budget for the whole run, not one per unit."""
    import asyncio

    from ingest import fetch as fetch_mod
    from ingest.http import shared_client

    shared_client.cache_clear()
    built: list[object] = []
    real_init = httpx.Client.__init__

    def _counting_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        built.append(self)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _counting_init)

    seen: list[httpx.Client] = []

    def _get(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        seen.append(self)
        return httpx.Response(200, content=b"bytes", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", _get)

    asyncio.run(fetch_mod.UriFetcher().fetch("https://source.test/a"))
    asyncio.run(fetch_mod.UriFetcher().fetch("https://source.test/b"))

    assert len(seen) == 2
    assert seen[0] is seen[1], "each unit fetch built its own client — one TCP+TLS handshake per object"
    assert len(built) == 1, f"{len(built)} clients constructed for two unit fetches"


def test_the_catalog_client_reuses_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ensure` alone makes up to four catalog calls; a client per call is four handshakes."""
    import pyarrow as pa

    from ingest.catalog_service import CatalogServiceClient
    from ingest.http import shared_client

    shared_client.cache_clear()
    seen: list[httpx.Client] = []

    def _post(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        seen.append(self)
        return httpx.Response(200, json={"location": "s3://b/p.lance", "version": 1}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", _post)

    client = CatalogServiceClient(pa.schema([pa.field("id", pa.string())]), base_url="http://catalog.test", token="t")
    client.ensure("bronze", "pages")

    assert len(seen) >= 2, f"expected several catalog calls, saw {len(seen)}"
    assert all(c is seen[0] for c in seen), "the catalog client builds a fresh connection per door"
    assert seen[0] is shared_client(), "the catalog client does not use the plane's pool"


def test_the_provenance_read_reuses_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A status poll every two seconds must not open a connection every two seconds."""
    from ingest.http import shared_client
    from ingest.provenance import LineageProvenanceReader

    shared_client.cache_clear()
    seen: list[httpx.Client] = []

    def _get(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        seen.append(self)
        return httpx.Response(200, json={"runs": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", _get)

    reader = LineageProvenanceReader()
    reader.has_run("r1")
    reader.has_run("r2")

    assert len(seen) == 2
    assert seen[0] is shared_client(), "the provenance read builds a fresh client per poll"
