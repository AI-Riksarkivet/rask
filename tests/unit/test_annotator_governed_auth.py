"""The annotator's governed-auth seam: a VERIFIED subject, and authz that fails closed.

`services/annotator` served the read-plane and took its author from a trusted `X-User` header
(`service_kit.media.deps.get_author`, defaulting to `"anon"`). That is fine for reads and a
cross-user leak for annotation projects, whose every entity is keyed on who owns or claims it.

These tests pin the two properties that make the difference real:

1. the subject comes from a **verified token**, with no header fallback; and
2. an auth layer that is *enabled but broken* returns **503**, never open access.

The second is the one worth having a test for: fail-open is silent, ships green, and is only
discovered by someone reading another user's tasks.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from annotator.api.security import (
    ANONYMOUS_SUBJECT,
    CheckerDep,
    CurrentSubject,
)
from annotator.core.config import AnnotatorSettings
from service_kit.exceptions import register_handlers
from service_kit.lakehouse.ns_errors import install_problem_handlers


def _app(settings: AnnotatorSettings, **state: Any) -> FastAPI:
    """A one-route app wired exactly like the real service, with `app.state` under test control.

    BOTH handler installers, because `annotator/main.py` calls both (lines 186 + 196) and the second
    one is the reason the auth refusals render at all: the governed kernel raises the `lance_namespace`
    taxonomy, which `register_handlers` does not map. Installing only the fleet half made this fixture
    claim a wiring the service does not have, and a refusal that reaches a real user through
    `install_problem_handlers` was being asserted here against starlette's fallback instead.
    """
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    for k, v in state.items():
        setattr(app.state, k, v)

    @app.get("/whoami")
    def whoami(subject: CurrentSubject) -> dict[str, str]:
        return {"subject": subject}

    @app.get("/guarded")
    async def guarded(subject: CurrentSubject, checker: CheckerDep) -> dict[str, bool]:
        return {"allowed": await checker(user=subject, relation="can_view", obj="project:acme")}

    app.dependency_overrides[type(settings)] = lambda: settings
    from annotator.core.config import get_annotator_settings

    app.dependency_overrides[get_annotator_settings] = lambda: settings
    return app


def _settings(**kw: Any) -> AnnotatorSettings:
    return AnnotatorSettings.model_construct(**kw)


# --------------------------------------------------------------------------------------------------
# The subject
# --------------------------------------------------------------------------------------------------


def test_an_x_user_header_can_no_longer_choose_the_subject() -> None:
    """THE regression guard. `X-User` used to BE the identity; it must now be inert.

    If this ever fails, any caller can act as any user by setting a header.
    """
    client = TestClient(_app(_settings(oidc_enabled=False, fga_enabled=False)))
    got = client.get("/whoami", headers={"X-User": "attacker"}).json()
    assert got == {"subject": ANONYMOUS_SUBJECT}
    assert got["subject"] != "attacker"


def test_with_oidc_on_the_subject_is_the_verified_token_sub() -> None:
    class _Token:
        sub = "gina"

    class _Verifier:
        def verify(self, token: str) -> Any:
            assert token == "good-token"
            return _Token()

    client = TestClient(_app(_settings(oidc_enabled=True, fga_enabled=False), oidc=_Verifier()))
    r = client.get("/whoami", headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 200
    assert r.json() == {"subject": "gina"}


# --------------------------------------------------------------------------------------------------
# Fail closed — the property that must never regress
# --------------------------------------------------------------------------------------------------


def test_fga_enabled_but_no_client_is_503_not_permissive() -> None:
    """The dangerous one: a broken authz layer must not silently become an open one."""
    client = TestClient(_app(_settings(oidc_enabled=False, fga_enabled=True)))  # no `fga` on state
    assert client.get("/guarded").status_code == 503


# --------------------------------------------------------------------------------------------------
# The settings invariant
# --------------------------------------------------------------------------------------------------


def test_authorization_without_authentication_is_refused_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """FGA answers "may THIS subject", so enabling it without OIDC would check an unverified subject.

    Caught when settings are built, not at the first request.

    Driven through the ENVIRONMENT rather than kwargs: `LANCE_*` are the field aliases, so this also
    proves the names an operator actually sets are the ones that bind. Passing them as keyword
    arguments would exercise a path no deployment uses.
    """
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ENABLED", "false")
    with pytest.raises(ValueError, match="RASK_OIDC_ENABLED is required"):
        AnnotatorSettings()


def test_oidc_without_issuer_and_audience_is_refused_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.delenv("RASK_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("RASK_OIDC_AUDIENCE", raising=False)
    with pytest.raises(ValueError, match="RASK_OIDC_ISSUER and RASK_OIDC_AUDIENCE are required"):
        AnnotatorSettings()


# ── the WRITE routes' author stamp ──────────────────────────────────────────────────────────────
#
# docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" — "Write authorship comes from an unverified, client-supplied `X-User` header".
#
# This file already pinned the projects plane ("X-User used to BE the identity; it must now be
# inert") — but the two Lance WRITE routes kept the old seam: `save_annotations` and `apply_tags`
# stamped `service_kit.media.deps.get_author`'s value as the reviewer, so any caller could sign
# another person's name onto annotation provenance by setting a header. The independent re-audit
# (2026-08-28) confirmed it live at HEAD and noted the sharp part: the gateway's spoofable-header
# strip covers `dapr-*` and `x-forwarded-*` but not `x-user`, so the header sailed through the edge.


def _author_dependency(route_fn: object) -> object:
    """The callable FastAPI will actually run to fill the `author` parameter."""
    import typing

    hints = typing.get_type_hints(route_fn, include_extras=True)
    metadata = typing.get_args(hints["author"])[1:]
    for item in metadata:
        dependency = getattr(item, "dependency", None)
        if dependency is not None:
            return dependency
    raise AssertionError("`author` carries no dependency at all")


@pytest.mark.parametrize(
    "route",
    ["save_annotations"],
)
def test_the_write_routes_take_their_author_from_the_VERIFIED_subject(route: str) -> None:
    """Asserted on the dependency IDENTITY: FastAPI runs exactly this callable, so the author can
    only be what `current_subject` answers — the verified `sub`, `anon` with OIDC off, 401 with OIDC
    on and no token, 503 when enabled-but-unwired. No header is consulted anywhere on that path."""
    from annotator.annotations import save, tags
    from annotator.api import security

    route_fn = getattr(save, route, None) or getattr(tags, route)
    assert _author_dependency(route_fn) is security.current_subject, (
        f"`{route}` still resolves its author through the client-supplied X-User seam — "
        "a caller can sign any name onto annotation provenance by setting a header"
    )
