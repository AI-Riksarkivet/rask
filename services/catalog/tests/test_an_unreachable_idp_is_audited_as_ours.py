"""The catalog's door charges an IdP it cannot reach to itself: a 503, audited `verifier_unavailable`.

`catalog.api.security.authenticate` is its own door, not `service_kit.governed.deps`'s, so the split that module makes
between `verify`'s two refusals is made here too. Without it every request during an IdP outage writes an
`invalid_token` record against a caller whose bearer was never read.

THE VERIFIER IS REAL, and its issuer is a loopback port nothing accepts on. The refusal is therefore the type `oidc.py`
actually raises — `service_kit.exceptions.ServiceUnavailableError`, a different class from the `lance_namespace` one
`security.py` raises for its own 503s — so a door catching the wrong one of the two stays red here.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterator

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api import security
from catalog.api.dependencies import SettingsDep
from catalog.core.config import Settings
from service_kit.exceptions import register_handlers
from service_kit.governed.audit import AUDIT_LOGGER
from service_kit.governed.oidc import OIDCVerifier
from service_kit.lakehouse.ns_errors import install_problem_handlers


AUDIENCE = "lance-catalog"


@pytest.fixture
def unreachable_issuer() -> Iterator[str]:
    """Bound and never listening, so a connect is refused at once and no other process can take the port meanwhile."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        yield f"http://127.0.0.1:{held.getsockname()[1]}"


def _client(verifier: OIDCVerifier, issuer: str) -> TestClient:
    """The catalog's door behind the handler pair `build_lance_service_app` installs, in its order."""
    settings = Settings.model_validate(
        {
            "LANCE_S3_ACCESS_KEY_ID": "x",
            "LANCE_S3_SECRET_ACCESS_KEY": "y",
            "RASK_OIDC_ENABLED": True,
            "RASK_OIDC_ISSUER": issuer,
            "RASK_OIDC_AUDIENCE": AUDIENCE,
            "RASK_OIDC_ALLOW_INSECURE": True,
        }
    )
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.oidc = verifier
    app.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings

    @app.get("/gated")
    def _gated(token: security.CurrentToken) -> dict[str, str | None]:
        return {"sub": token.sub if token is not None else None}

    return TestClient(app, raise_server_exceptions=False)


def _bearer(issuer: str) -> dict[str, str]:
    token = jwt.encode({"iss": issuer, "sub": "gina", "aud": AUDIENCE, "iat": 0, "exp": 1 << 31}, "unverified", algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def audit_trail(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    return caplog


def _audited_reasons(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [getattr(record, "audit.reason", None) for record in caplog.records if record.name == AUDIT_LOGGER]


def test_an_issuer_the_catalog_cannot_reach_is_a_503_audited_as_ours(unreachable_issuer: str, audit_trail: pytest.LogCaptureFixture) -> None:
    verifier = OIDCVerifier(unreachable_issuer, AUDIENCE, cache_ttl=300, allow_insecure=True)

    response = _client(verifier, unreachable_issuer).get("/gated", headers=_bearer(unreachable_issuer))

    assert response.status_code == 503, response.text
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


def test_a_token_from_an_issuer_the_catalog_does_not_trust_is_still_the_callers(unreachable_issuer: str, audit_trail: pytest.LogCaptureFixture) -> None:
    """The control: the split must not turn a refused token into an outage. Two issuers, because with one
    `_provider_for` resolves without reading the token and this branch cannot be reached."""
    verifier = OIDCVerifier([unreachable_issuer, f"{unreachable_issuer}/second"], AUDIENCE, cache_ttl=300, allow_insecure=True)

    response = _client(verifier, unreachable_issuer).get("/gated", headers=_bearer("https://elsewhere.example.test"))

    assert response.status_code == 401, response.text
    assert _audited_reasons(audit_trail) == ["invalid_token"]
