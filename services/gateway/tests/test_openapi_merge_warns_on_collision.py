"""A colliding path/schema between two backends must not vanish silently from the merged spec.

`GW-OPENAPI-SILENT-SHADOW`. `_merged_openapi` folded every backend's spec together with plain
`dict.update`, so a key two services both define — every service's `{prefix}/health`, a shared schema
name — had the later target silently overwrite the earlier, no log line, no namespacing. The merge is
still last-writer-wins (deterministic in target order), but a shadowed key now leaves a warning an
operator can see.
"""

from __future__ import annotations

import importlib
import logging

import httpx
import pytest


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    import gateway

    return importlib.reload(gateway)


class _SpecByBase:
    """Answers `.get` from a {base-url-prefix: spec} map, so each target gets its own spec."""

    def __init__(self, by_base: dict[str, dict]) -> None:
        self._by_base = by_base

    async def get(self, url: str, timeout: float | None = None) -> httpx.Response:
        for base, spec in self._by_base.items():
            if url.startswith(base):
                return httpx.Response(200, json=spec, request=httpx.Request("GET", url))
        raise AssertionError(f"no stub spec for {url}")


@pytest.mark.asyncio
async def test_collision_is_warned_and_both_keys_survive_last_writer(gw, caplog: pytest.LogCaptureFixture) -> None:
    targets = [("alpha", "http://alpha"), ("beta", "http://beta")]
    alpha = {
        "openapi": "3.1.0",
        "paths": {"/api/health": {"get": {"summary": "alpha health"}}, "/api/alpha": {}},
        "components": {"schemas": {"Shared": {"title": "alpha"}, "AlphaOnly": {}}},
    }
    beta = {
        "openapi": "3.1.0",
        "paths": {"/api/health": {"get": {"summary": "beta health"}}, "/api/beta": {}},
        "components": {"schemas": {"Shared": {"title": "beta"}, "BetaOnly": {}}},
    }
    client = _SpecByBase({"http://alpha": alpha, "http://beta": beta})

    with caplog.at_level(logging.WARNING, logger="gateway"):
        merged = await gw._merged_openapi(client, "/api", targets)

    warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "/api/health" in warnings, f"the shadowed path was not warned about: {warnings!r}"
    assert "Shared" in warnings, f"the shadowed schema was not warned about: {warnings!r}"

    # Last-writer-wins is preserved: the non-colliding keys of both backends are present, and the
    # collision resolves to the later target (beta), exactly as the old dict.update did.
    paths = merged.paths
    assert {"/api/health", "/api/alpha", "/api/beta"} <= set(paths)
    assert paths["/api/health"]["get"]["summary"] == "beta health"
