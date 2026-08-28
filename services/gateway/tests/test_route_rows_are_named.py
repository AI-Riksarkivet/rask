"""Route rows carry named fields, and the merged spec is a typed model — not positional tuples.

`GW-ROUTE-TUPLE`. `Route` was `tuple[str, str, str, str]` read by index (`route[0]`) and unpacked
positionally at four sites, and `_merged_openapi` returned a bare `dict`. A row is a NamedTuple now,
so a field is named where it is read, and the merge returns a declared model instead of an untyped
dict. Both remain wire-compatible: the NamedTuple still unpacks positionally, and the model still
serialises to the same JSON.
"""

from __future__ import annotations

import importlib

import httpx
import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    import gateway

    return importlib.reload(gateway)


def test_route_rows_expose_named_fields(gw) -> None:
    row = gw._pick_route("/api/ray/jobs", gw._routes())
    assert row is not None
    # Named access — a plain 4-tuple raises AttributeError on every one of these.
    assert row.public_prefix == "/api/ray"
    assert row.upstream_prefix == "/api/ray"
    assert row.app_id == "compute"
    assert row.fallback_url.endswith(":8804")
    # Still a tuple: positional unpacking (used across the module) keeps working.
    public, upstream, app_id, fallback = row
    assert (public, upstream, app_id, fallback) == (row.public_prefix, row.upstream_prefix, row.app_id, row.fallback_url)


class _EmptyClient:
    async def get(self, url: str, timeout: float | None = None) -> httpx.Response:
        return httpx.Response(200, json={"openapi": "3.1.0", "paths": {}, "components": {"schemas": {}}}, request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_merged_openapi_returns_a_typed_model(gw) -> None:
    from pydantic import BaseModel

    result = await gw._merged_openapi(_EmptyClient(), "/api", [("alpha", "http://alpha")])
    assert isinstance(result, BaseModel), f"_merged_openapi still returns a bare {type(result).__name__}"
    dumped = result.model_dump(mode="json")
    assert set(dumped) >= {"openapi", "info", "paths", "components"}
