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

from typing import Any

import pytest
from annotator.api.security import (
    ANONYMOUS_SUBJECT,
    CheckerDep,
    CurrentSubject,
)
from annotator.core.config import AnnotatorSettings
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers


def _app(settings: AnnotatorSettings, **state: Any) -> FastAPI:
    """A one-route app wired exactly like the real service, with `app.state` under test control."""
    app = FastAPI()
    register_handlers(app)
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


def test_with_oidc_off_the_subject_is_anon_so_a_dev_stack_still_works() -> None:
    client = TestClient(_app(_settings(oidc_enabled=False, fga_enabled=False)))
    assert client.get("/whoami").json() == {"subject": ANONYMOUS_SUBJECT}


def test_an_x_user_header_can_no_longer_choose_the_subject() -> None:
    """THE regression guard. `X-User` used to BE the identity; it must now be inert.

    If this ever fails, any caller can act as any user by setting a header.
    """
    client = TestClient(_app(_settings(oidc_enabled=False, fga_enabled=False)))
    got = client.get("/whoami", headers={"X-User": "attacker"}).json()
    assert got == {"subject": ANONYMOUS_SUBJECT}
    assert got["subject"] != "attacker"


def test_with_oidc_on_a_request_without_a_token_is_401() -> None:
    class _Verifier:
        def verify(self, token: str) -> Any:  # pragma: no cover - not reached
            raise AssertionError("must not be called without credentials")

    client = TestClient(_app(_settings(oidc_enabled=True, fga_enabled=False), oidc=_Verifier()))
    assert client.get("/whoami").status_code == 401


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


def test_a_token_the_verifier_rejects_does_not_authenticate() -> None:
    class _Verifier:
        def verify(self, token: str) -> Any:
            raise ValueError("bad signature")

    client = TestClient(_app(_settings(oidc_enabled=True, fga_enabled=False), oidc=_Verifier()))
    with pytest.raises(ValueError, match="bad signature"):
        client.get("/whoami", headers={"Authorization": "Bearer forged"})


# --------------------------------------------------------------------------------------------------
# Fail closed — the property that must never regress
# --------------------------------------------------------------------------------------------------


def test_oidc_enabled_but_no_verifier_is_503_not_open() -> None:
    """Discovery failed at startup, or a deployment skew. 503 is the honest answer."""
    client = TestClient(_app(_settings(oidc_enabled=True, fga_enabled=False)))  # no `oidc` on state
    assert client.get("/whoami").status_code == 503


def test_fga_enabled_but_no_client_is_503_not_permissive() -> None:
    """The dangerous one: a broken authz layer must not silently become an open one."""
    client = TestClient(_app(_settings(oidc_enabled=False, fga_enabled=True)))  # no `fga` on state
    assert client.get("/guarded").status_code == 503


def test_fga_off_is_permissive_so_an_offline_stack_behaves_as_before() -> None:
    client = TestClient(_app(_settings(oidc_enabled=False, fga_enabled=False)))
    assert client.get("/guarded").json() == {"allowed": True}


# --------------------------------------------------------------------------------------------------
# The settings invariant
# --------------------------------------------------------------------------------------------------


def test_authorization_without_authentication_is_refused_at_construction() -> None:
    """FGA answers "may THIS subject", so enabling it without OIDC would check an unverified subject.

    Caught when settings are built, not at the first request.
    """
    with pytest.raises(ValueError, match="LANCE_OIDC_ENABLED is required"):
        AnnotatorSettings(LANCE_FGA_ENABLED=True, LANCE_OIDC_ENABLED=False)  # type: ignore[call-arg]


def test_oidc_without_issuer_and_audience_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="LANCE_OIDC_ISSUER and LANCE_OIDC_AUDIENCE are required"):
        AnnotatorSettings(LANCE_OIDC_ENABLED=True)  # type: ignore[call-arg]
