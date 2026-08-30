"""The gateway's configuration is a MODEL, read once — not 16 `os.environ` calls (FLEET-ENV-SCATTER).

The gateway was the only service in the fleet with no settings class at all: sixteen raw
`os.environ.get()` reads spread across `_routes()`, `_target_base()`, `_dapr_enabled()`, the
sidecar-route blocklist, the lifespan and module import, several of them evaluated PER REQUEST. Two
consequences, and neither is style:

* **`.env` reached only some of them.** `_routes()` called `load_dotenv()`; the `RASK_DOCS` read did
  not, and it runs at IMPORT — before anything has called `_routes()`. So a `.env` that opens the
  docs was silently ignored, while a `.env` that set `RASK_API_PREFIX` was honoured. One config
  file, two answers, decided by which line happened to execute first.
* **Config was re-decided per request.** `_target_base()` read `RASK_DAPR_ENABLED` and
  `DAPR_HTTP_PORT` on every proxied call, so mutating the process environment mid-flight silently
  re-routed live traffic between the Dapr sidecar and the direct upstream. Startup configuration
  that can change under a running server is configuration nothing can reason about.

Both are pinned here against `GatewaySettings`, which reads `.env` itself (pydantic-settings, the
same mechanism `FlowsSettings` and `ControlplaneSettings` use) and is built ONCE onto
`app.state.settings`.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi.testclient import TestClient


_ENV_KNOBS = ("RASK_DOCS", "RASK_API_PREFIX", "RASK_DAPR_ENABLED", "DAPR_HTTP_PORT", "RASK_COMPUTE_URL")


def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_KNOBS:
        monkeypatch.delenv(name, raising=False)


def test_a_dotenv_file_configures_every_knob_not_only_the_ones_read_late(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One config file, one answer. `RASK_DOCS` is the knob the import-time read could never see."""
    _clean(monkeypatch)
    (tmp_path / ".env").write_text("RASK_API_PREFIX=/api\nRASK_DOCS=true\nRASK_COMPUTE_URL=http://compute.dotenv:9000\n")
    monkeypatch.chdir(tmp_path)

    import gateway

    gw = importlib.reload(gateway)

    settings = gw.build_gateway_settings()
    assert settings.api_prefix == "/api"
    assert settings.docs_enabled is True, "the docs knob was read at import, before any .env was loaded"
    assert settings.compute_url == "http://compute.dotenv:9000"
    # And the app built from that file actually serves them.
    assert gw.app.openapi_url == "/openapi.json"


def test_the_route_table_and_the_prefix_come_from_one_read(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`app.state.api_prefix` and the route rows cannot disagree — they share one settings object."""
    _clean(monkeypatch)
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    gw = importlib.reload(gateway)

    with TestClient(gw.app) as client:
        state = client.app.state  # type: ignore[attr-defined]
        assert state.settings.api_prefix == state.api_prefix
        assert all(row.public_prefix.startswith(state.api_prefix) for row in state.routes)


def test_the_dapr_lane_is_decided_at_startup_not_per_request(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flipping the environment under a running server must not re-route live traffic."""
    _clean(monkeypatch)
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_DAPR_ENABLED", "false")
    import gateway

    gw = importlib.reload(gateway)

    seen: list[str] = []

    class _Streamed(httpx.AsyncByteStream):
        """The gateway `aiter_raw()`s the upstream response — a pre-read body cannot be streamed."""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"{}"

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, stream=_Streamed(), headers={"content-length": "2"}, request=request)

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client.get("/api/ray/jobs")
        # The operator's shell changes underneath the running process. It must change nothing.
        monkeypatch.setenv("RASK_DAPR_ENABLED", "true")
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        client.get("/api/ray/jobs")

    assert len(seen) == 2
    assert all("/v1.0/invoke/" not in url for url in seen), f"a live request was re-routed through the sidecar mid-flight: {seen}"
