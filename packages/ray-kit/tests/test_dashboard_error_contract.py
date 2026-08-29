"""What `ray_kit.dashboard` tells a caller when a call FAILS — four separate defects.

PS-17 — every status >= 400 from Ray's log-file endpoint was reported as `ok=True` with the note
"(empty or unavailable)". A token-authed cluster answering 401 therefore rendered as an EMPTY LOG
PANE: no error, nothing red, and the one fact an operator needs (the credential is wrong) discarded
at the seam. The 5xx half of that branch is genuine — Ray's log endpoint really does 500 on an empty
file and offers no other way to tell — but a 4xx is never "empty", it is about us.

PS-18 — `job_logs` was the only SDK call in the module caught with a bare `except Exception`; its six
siblings catch `RAY_TRANSIENT_ERRORS`. A `TypeError` or an `AttributeError` in our own code therefore
came back as a polite `ok=False` payload that reads exactly like an unreachable cluster.

PS-20 — the error string `f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN]` was copy-pasted NINE
times, so the truncation was one omission away from being inconsistent — and PS-21 is that omission.

PS-21 — `proxy` alone returned the raw, untruncated exception text to the BROWSER, so an httpx error
that names the internal dashboard address published it to whoever loaded the iframe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from ray.job_submission import JobSubmissionClient

from ray_kit import dashboard


_DASH = "http://ray-head.rask-internal.svc:8265"


class _StubJobClient:
    """Structural stand-in for `JobSubmissionClient` — only `get_job_info` is exercised."""

    def __init__(self, info: Any = None, raises: BaseException | None = None) -> None:
        self._info = info
        self._raises = raises

    def get_job_info(self, submission_id: str) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._info


class _Info:
    def __init__(self, node_id: str) -> None:
        self.driver_node_id = node_id


def _client(**kwargs: Any) -> JobSubmissionClient:
    """A structural stand-in — `job_logs` only ever calls `get_job_info`, and constructing the real
    SDK client issues HTTP. Cast, not `ignore`: the substitution is the test's own claim."""
    return cast(JobSubmissionClient, _StubJobClient(**kwargs))


def _responder(status: int, body: str = "") -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=body))


# ── PS-17 ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_logs_reports_a_401_as_a_failure_not_as_an_empty_file() -> None:
    async with httpx.AsyncClient(transport=_responder(401, "unauthorized")) as http:
        payload = await dashboard.logs(http, _DASH, "node-1", "worker.log")
    assert payload.ok is False, "a rejected credential rendered as an empty log pane"
    assert payload.error and "401" in payload.error
    assert payload.text is None


@pytest.mark.asyncio
async def test_logs_reports_a_404_filename_as_a_failure() -> None:
    async with httpx.AsyncClient(transport=_responder(404, "no such file")) as http:
        payload = await dashboard.logs(http, _DASH, "node-1", "typo.log")
    assert payload.ok is False
    assert payload.error and "404" in payload.error


@pytest.mark.asyncio
async def test_logs_keeps_the_clean_note_for_rays_empty_file_500() -> None:
    """The documented Ray quirk survives: an empty file 500s and must not read as an outage."""
    async with httpx.AsyncClient(transport=_responder(500)) as http:
        payload = await dashboard.logs(http, _DASH, "node-1", "empty.log")
    assert payload.ok is True
    assert payload.text == "(empty or unavailable)"


@pytest.mark.asyncio
async def test_job_logs_reports_a_401_as_a_failure_not_as_an_empty_file() -> None:
    client = _client(info=_Info("node-1"))
    async with httpx.AsyncClient(transport=_responder(403, "forbidden")) as http:
        payload = await dashboard.job_logs(http, client, _DASH, "sub-1")
    assert payload.ok is False, "a rejected credential rendered as an empty driver log"
    assert payload.error and "403" in payload.error


# ── PS-18 ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_logs_lets_a_programming_error_out_instead_of_reporting_ray_down() -> None:
    client = _client(raises=TypeError("get_job_info() got an unexpected keyword argument"))
    async with httpx.AsyncClient(transport=_responder(200, "logs")) as http:
        with pytest.raises(TypeError):
            await dashboard.job_logs(http, client, _DASH, "sub-1")


@pytest.mark.asyncio
async def test_job_logs_still_answers_an_unknown_submission_id_politely() -> None:
    """Ray's SDK raises a plain `RuntimeError` for an id it does not know — that stays a payload."""
    client = _client(raises=RuntimeError("Job sub-nope does not exist"))
    async with httpx.AsyncClient(transport=_responder(200, "logs")) as http:
        payload = await dashboard.job_logs(http, client, _DASH, "sub-nope")
    assert payload.ok is False
    assert payload.error and "RuntimeError" in payload.error


# ── PS-20 ────────────────────────────────────────────────────────────────────────────────────


def test_the_error_text_helper_is_the_one_formatter() -> None:
    text = dashboard._error_text(ValueError("x" * 1000))
    assert text.startswith("ValueError: ")
    assert len(text) == dashboard._ERROR_MSG_MAX_LEN

    source = Path(str(dashboard.__file__)).read_text(encoding="utf-8")
    assert source.count('f"{type(exc).__name__}: {exc!s}"') == 1, "the error-payload expression is copy-pasted; it belongs in `_error_text`"


# ── PS-21 ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_proxy_does_not_hand_the_browser_the_internal_dashboard_address() -> None:
    leaky = httpx.ConnectError(f"[Errno 111] Connection refused connecting to {_DASH}/api/serve/applications/ " + "detail " * 200)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise leaky

    async with httpx.AsyncClient(transport=httpx.MockTransport(_boom)) as http:
        resp = await dashboard.proxy(http, _DASH, "api/serve/applications/", "GET", "", {}, b"")

    assert resp.status_code == 502
    assert b"rask-internal" not in resp.content, "the proxy published the internal dashboard address to the browser"
    assert len(resp.content) <= dashboard._ERROR_MSG_MAX_LEN, "the proxy is the one error return with no bound on its length"
