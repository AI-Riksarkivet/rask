"""`dashboard.proxy` must not forward a path that can escape the prefix it was asked to serve.

The Serve proxy exists to forward ONE prefix of the Ray dashboard (`/api/serve/*`). It builds the
upstream URL as `f"{dashboard_url}/{path}"`, and a dot-segment in `path` is resolved by the HTTP
client against the dashboard ORIGIN — so `api/serve/../v0/logs/file/` becomes a request for
`/api/v0/logs/file/`, any GET the dashboard serves: the job list, node log files, cluster state.

The public edge is not the defence. The gateway collapses `.`/`..` before routing, and a raw `..` in
a request line is normalised by most clients before it is sent — but a PERCENT-ENCODED one is not.
Reproduced 2026-09-02 through the compute app offline: `GET /api/serve/%2e%2e/v0/logs/file` arrived at
the handler decoded, and the seam forwarded `api/serve/../v0/logs/file/`. Any caller that reaches the
service port directly (every pod in the namespace, under the default network policy) has the whole
dashboard.

Refused HERE, at the seam that builds the URL, rather than in the one consumer that happens to call
it today. Empty segments are refused for the same reason: `a//..` is a second spelling of the same
escape once a normaliser runs.
"""

from __future__ import annotations

import httpx
import pytest

from ray_kit import dashboard


def _recording_client(seen: list[httpx.Request]) -> httpx.AsyncClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


@pytest.mark.parametrize(
    "path",
    [
        "api/serve/../v0/logs/file/",
        "api/serve/./applications/",
        "api/serve/a/../../v0/x/",
        "api/serve//applications/",
        "../v0/logs/file/",
        # A double-encoded segment survives one decode as `%2e%2e`; whether the upstream decodes it
        # again is the upstream's business, and the seam refuses it rather than finding out.
        "api/serve/%2e%2e/v0/logs/file/",
        "api/serve/%2E%2E/v0/logs/file/",
    ],
)
@pytest.mark.asyncio
async def test_a_path_with_a_dot_or_empty_segment_is_refused_and_never_forwarded(path: str) -> None:
    seen: list[httpx.Request] = []
    async with _recording_client(seen) as http:
        resp = await dashboard.proxy(http, "http://ray:8265", path, "GET", "", {}, b"")
    assert resp.status_code == 400, f"{path!r} was answered {resp.status_code}"
    assert seen == [], f"{path!r} was FORWARDED to the dashboard as {seen[0].url if seen else None}"


@pytest.mark.asyncio
async def test_an_ordinary_serve_path_still_forwards() -> None:
    """A refusal that swallowed everything would pass the test above."""
    seen: list[httpx.Request] = []
    async with _recording_client(seen) as http:
        resp = await dashboard.proxy(http, "http://ray:8265", "api/serve/applications/", "GET", "", {}, b"")
    assert resp.status_code == 200
    assert [str(r.url) for r in seen] == ["http://ray:8265/api/serve/applications/"]
