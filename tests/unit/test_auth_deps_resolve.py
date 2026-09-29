"""A gated route must actually GATE — not silently become a query parameter.

THE BUG THIS EXISTS FOR, found 2026-08-04 while gating the object routes (#90).

`make_auth_deps(settings_dep)` annotates its inner dependencies with `settings_dep`, a LOCAL name.
`service_kit/governed/deps.py` used to carry `from __future__ import annotations`, which turned that
into the string `"settings_dep"` — and FastAPI resolves annotations with `get_type_hints` against the
DEFINING MODULE's globals, where no such name exists.

The failure was silent in the worst way. FastAPI does not raise on an unresolved ForwardRef; it
demotes the parameter to a **query param**. So every route depending on the auth deps answered

    422 {"field": "query.settings", "message": "Field required"}

instead of authorizing, and the checker never ran at all. That is a broken gate presenting as a
validation error, on the routes whose entire purpose is authorization.

WHY THE EXISTING TESTS DID NOT CATCH IT. `test_viewer_dataset_authz.py` overrides
`CheckerDep.__metadata__[0].dependency` with `lambda: checker`. An override REPLACES the
sub-dependency whose signature is the broken one, so FastAPI never analyses it and the suite passes
against a route that could not work in production. The corpus-list gate shipped in that state.

So this file deliberately overrides NOTHING. It builds a bare app and asserts on the resolved
dependant, which is the only view that shows what FastAPI will actually do with a real request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from service_kit.governed.deps import make_auth_deps


class _Settings(BaseModel):
    oidc_enabled: bool = False
    fga_enabled: bool = False


def _settings() -> _Settings:
    return _Settings()


SettingsDep = Annotated[_Settings, Depends(_settings)]

# MODULE level, mirroring how `viewer/api/security.py` publishes its deps — and the first cut of this
# file got it wrong in the same way the bug under test does. With `deps` local to the app factory,
# this module's own `from __future__ import annotations` made `Depends(deps.current_subject)`
# unresolvable and the route 422'd on `query.subject`. The test reproduced the defect it was written
# to catch, one level up. Endpoint modules bind their deps at module scope for exactly this reason.
_deps = make_auth_deps(SettingsDep)
CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[object, Depends(_deps.get_checker)]


def _app_with_a_gated_route() -> FastAPI:
    app = FastAPI()

    @app.get("/gated")
    async def gated(subject: CurrentSubject, checker: CheckerDep) -> dict[str, str]:
        assert checker is not None  # the checker must be INJECTED, not defaulted away
        return {"subject": subject}

    return app


def test_a_gated_route_answers_rather_than_422ing() -> None:
    """The behavioural half. With OIDC/FGA off the checker is permissive, so a bare call must reach
    the handler — a 422 here means the gate is not a gate, it is a broken signature."""
    from fastapi.testclient import TestClient

    r = TestClient(_app_with_a_gated_route()).get("/gated")

    assert r.status_code == 200, r.text
    assert r.json() == {"subject": "anon"}
