"""An unhandled exception must answer problem+json, and a governed error must not read as a crash.

open_fastapi-audit — "Four fleet apps install no catch-all, so any unhandled exception answers
text/plain — a third error envelope no client can parse".

TWO THINGS, and the second is the load-bearing one.

**The catch-all.** `register_handlers` mapped exactly two classes — `DomainError` and
`RequestValidationError` — so anything else fell through to starlette's default: a `text/plain` 500
reading "Internal Server Error". That is the framework default for any app with no `Exception`
handler, so on its own it is not a rask choice. It is still a third envelope on services whose every
other error is RFC 9457, and the fix is one handler in the place the whole fleet already shares.

**The one that actually misreports.** The four `make_service_app`-only apps (compute, controlplane,
flows, notifications) cannot map `lance_namespace` errors at all — and the shared governed kernel they
call RAISES them: `governed/fga.py:507` raises `ServiceUnavailableError("authorization service
unavailable")` under a docstring promising "outage → ServiceUnavailableError → 503". In notifications
that contract silently did not hold. It still fails CLOSED — the refusal happens, nothing is
disclosed, no door opens — so this is a misreported outage, not an exposure. But
`api/visibility.py` answers the same class of failure two different ways in ONE function: its
`service_kit.exceptions.ServiceUnavailableError` became a 503 problem+json while the one
`fga.batch_check` raises became a text/plain 500.

The fix follows `ingest/__init__.py`, which already installs both handler sets on a
`make_service_app` app and says why in a comment: "A DENIAL MUST BE A 403, NOT A 500." Doing it in
the factory rather than per app also settles the divergence `open_python-audit` X11 files — ingest's
422 differing from its three fleet siblings — by making all of them the same shape rather than by
removing the handler that made ingest right.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError as NsServiceUnavailableError

from service_kit import make_service_app
from service_kit.exceptions import register_handlers


PROBLEM_JSON = "application/problem+json"


def _client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False: we are asserting on the RESPONSE the client sees, which is the
    # whole point. With it on, TestClient re-raises and there is no envelope to inspect.
    return TestClient(app, raise_server_exceptions=False)


def test_register_handlers_installs_a_catch_all() -> None:
    app = FastAPI()
    register_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("a secret path /var/lib/rask/creds and a stack frame")

    response = _client(app).get("/boom")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON), (
        f"an unhandled exception answered {response.headers['content-type']} — a third error envelope on a service whose every other error is RFC 9457"
    )


def test_the_catch_all_leaks_no_internals() -> None:
    """The reference is explicit: internals leak via logs only, never the body."""
    app = FastAPI()
    register_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("/var/lib/rask/creds")

    body = _client(app).get("/boom").text
    assert "/var/lib/rask/creds" not in body, f"the 500 body carried the exception text: {body}"
    assert "Traceback" not in body


def _factory_app(exc: Exception) -> FastAPI:
    app = make_service_app(title="probe", routers=[])

    @app.get("/raise")
    async def _raise() -> None:
        raise exc

    return app


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(NsServiceUnavailableError("authorization service unavailable"), id="lance-namespace"),
        pytest.param(RuntimeError("boom"), id="unhandled"),
    ],
)
def test_a_factory_built_app_answers_problem_json(exc: Exception) -> None:
    """compute, controlplane, flows and notifications are built exactly this way."""
    response = _client(_factory_app(exc)).get("/raise")
    assert response.headers["content-type"].startswith(PROBLEM_JSON), (
        f"a {type(exc).__name__} from a make_service_app app answered "
        f"{response.headers['content-type']} — the governed kernel these apps call raises "
        f"lance_namespace errors, and none of them could map one"
    )


def test_an_fga_outage_reads_as_503_not_500() -> None:
    """The contract `governed/fga.py` documents — "outage → ServiceUnavailableError → 503" — must
    actually hold in the apps that call it, or an outage is indistinguishable from a crash."""
    response = _client(_factory_app(NsServiceUnavailableError("authorization service unavailable"))).get("/raise")
    assert response.status_code == 503, (
        f"an FGA outage answered {response.status_code}; monitoring cannot tell a dependency being down from this service being broken"
    )
