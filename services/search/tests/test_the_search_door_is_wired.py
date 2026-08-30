"""The search service's authorization gate has to be CONNECTED to something.

A regression in my own X6 fix (`3593381c`), caught by an adversarial re-verification of the audit
backlog rather than by any test — including the one I wrote for X6.

X6 gave search everything except the line that makes it work: `SearchSettings` mixes in
`GovernedAuthSettings`, `api/security.py` builds the deps, every route declares the gate, and the
chart ships `LANCE_OIDC_*`/`LANCE_FGA_*`. But `main.py::lifespan` never set `app.state.fga`, and
`make_auth_deps.get_checker` is fail-CLOSED by design: FGA enabled with no client is
`ServiceUnavailableError`. So on an auth-enabled estate every search route answered **503**. The door
was shut, not guarded — which is safe, and is not what shipping an authorization seam means.

THE ESTATE HAD ALREADY PAID FOR THIS EXACT BUG, six days earlier, and left a note.
`services/compute/src/compute/lifespan.py:34-37`, above its own `attach_auth` call: "answers 503
'Authentication is enabled but unavailable' — which is fail-CLOSED and correct, and also means the
service does nothing. Measured on the live estate: that is exactly what shipped when the settings and
the gate landed without this line." I wrote the same shape into a third service without reading it.

WHY X6'S OWN TEST COULD NOT SEE IT: `test_search_is_governed.py` overrides
`security._deps.current_subject` and `security._deps.get_checker` to drive the decision — the two
dependencies that would have 503'd. Overriding the thing under test is how a gate can be proven to
DECIDE correctly while never being proven to be REACHED. So this file runs the real lifespan.

The disposal half comes with it: the lifespan's `yield` was bare, so an exception past it skipped the
resource loop, and the FGA client was never closed at all — the leak `service_kit.governed.fga.
dispose` exists to prevent, and which five siblings already call.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from search.core.config import SearchSettings


def _governed_settings() -> SearchSettings:
    """FGA on, and pre-provisioned so `attach_auth` builds a client without touching the network."""
    return SearchSettings.model_validate(
        {
            "LANCE_FGA_ENABLED": True,
            "LANCE_OIDC_ENABLED": True,
            "LANCE_OIDC_ISSUER": "https://issuer.test",
            "LANCE_OIDC_AUDIENCE": "rask",
            "LANCE_FGA_STORE_ID": "01JSTORE",
            "LANCE_FGA_MODEL_ID": "01JMODEL",
        }
    )


@pytest.fixture
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the lifespan off S3 and off the network; the auth wiring is what is under test."""
    from search import main as main_mod
    from service_kit.media import lifespan as media_lifespan_mod

    # `search.main._settings` resolves `get_search_settings` from this module's globals at LIFESPAN
    # time, so the patch reaches the real lifespan rather than a value captured at import.
    monkeypatch.setattr(main_mod, "get_search_settings", _governed_settings)
    # `dataset_handle` moved with the lifespan itself (DUP-16): the three media services share one
    # implementation in `service_kit.media.lifespan`, which is where the open now happens.
    monkeypatch.setattr(media_lifespan_mod, "dataset_handle", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no dataset in this test")))


@pytest.mark.asyncio
async def test_the_lifespan_builds_the_authorization_client(hermetic: None) -> None:
    """The regression itself. Without this the gate is reachable and answers 503 forever."""
    from search.main import lifespan

    app = FastAPI()
    async with lifespan(app):
        assert getattr(app.state, "fga", None) is not None, (
            "the search lifespan left app.state.fga unset while FGA is enabled — every gated route "
            "answers 503 'Authorization is enabled but unavailable', so the service is shut, not guarded"
        )
        assert getattr(app.state, "oidc", None) is not None, "no OIDC verifier, so no subject can be verified either"


@pytest.mark.asyncio
async def test_the_gate_decides_rather_than_503s(hermetic: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the wiring is FOR: a real checker resolved from real app state, reaching a real verdict."""
    from search.api import security
    from search.main import lifespan
    from service_kit.governed import fga as fga_mod

    async def check(_client: object, *, user: str, relation: str, obj: str) -> bool:
        return False

    monkeypatch.setattr(fga_mod, "check", check)

    app = FastAPI()
    async with lifespan(app):
        request = type("R", (), {"app": app})()
        checker = security._deps.get_checker(request, _governed_settings())
        assert await checker(user="eve", relation="can_read_data", obj="corpus:x") is False, (
            "the checker did not reach a verdict — it either 503'd or fell back to permissive"
        )


@pytest.mark.asyncio
async def test_the_client_is_disposed_on_shutdown(hermetic: None) -> None:
    """`fga.dispose` exists because five lifespans built a client and one closed it. Six now.

    Asserted through the REAL disposer by swapping in a client that records its own close, rather
    than by patching `search.main`'s binding — patching the name would prove only that a call site
    exists, and the point is that the client actually gets closed.
    """
    from search.main import lifespan

    class _Spy:
        closed = False

        async def close(self) -> None:
            type(self).closed = True

    app = FastAPI()
    async with lifespan(app):
        assert app.state.fga is not None
        app.state.fga = _Spy()
    assert _Spy.closed, "the search lifespan never closed its FGA client — the aiohttp session leaks one half-open connection per replica"
