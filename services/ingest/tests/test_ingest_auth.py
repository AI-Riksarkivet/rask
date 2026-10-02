"""The ingest control API is not a public door.

It shipped with NO authentication. That is worse than an ordinary missing gate, because the endpoint
takes a caller-supplied SOURCE: with `local-dir` it was one anonymous request to read the ingest pod's
own filesystem into a governed table (the confinement in `adapters.py` closed that primitive), and
with any kind it is an anonymous writer into a tenant's bronze tier and an anonymous READER of run
records that name a project, its datasets, its source keys and its errors.

Both doors are asserted here, and both fail closed. The shape is deliberately the medallion's
`authorize_produce`: same question, so it must not have a second, weaker answer. The service door verifies a real
projected service-account token against the root conftest's loopback issuer ([[LH-220]], D1).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Annotated, Any

import pytest
from fastapi import FastAPI, Header, Request
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError, UnauthenticatedError

from ingest.auth import AuthSettingsDep, IngestAuthSettings, authorize_ingest, get_auth_settings
from service_kit.governed.machine_identity import ServiceAccountVerifier
from service_kit.lakehouse.ns_errors import install_problem_handlers


#: The door's audience and the one account its subject map names.
AUDIENCE = "rask-ingest"
PRODUCER_ACCOUNT = "rask-sa-medallion-producer"


@pytest.fixture
def service_door(sa_issuer: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[ServiceAccountVerifier]:
    """The service-account door configured as the chart configures it, and the verifier `attach_auth` builds from it."""
    monkeypatch.setenv("RASK_SA_ISSUER", sa_issuer.issuer)
    monkeypatch.setenv("RASK_SA_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("RASK_SA_SUBJECTS", json.dumps({f"system:serviceaccount:default:{PRODUCER_ACCOUNT}": "service-medallion-producer"}))
    monkeypatch.setenv("RASK_SA_FETCH_TOKEN_FILE", str(sa_issuer.fetch_token_file))
    monkeypatch.setenv("RASK_SA_CA_FILE", str(sa_issuer.ca_file))
    settings = IngestAuthSettings()
    assert settings.sa_issuer and settings.sa_audience
    yield ServiceAccountVerifier(
        settings.sa_issuer,
        settings.sa_audience,
        settings.sa_subjects,
        cache_ttl=settings.oidc_cache_ttl,
        leeway=settings.oidc_leeway,
        fetch_token_file=settings.sa_fetch_token_file,
        ca_file=settings.sa_ca_file,
    )


def _service_bearer(sa_issuer: Any, account: str = PRODUCER_ACCOUNT) -> dict[str, str]:
    return {"authorization": f"Bearer {sa_issuer.mint(account, audience=AUDIENCE)}"}


def _app(*, oidc: object = None, fga: object = None, sa_oidc: ServiceAccountVerifier | None = None, service_project: str = "demo") -> FastAPI:
    """A minimal app carrying only what the door reads."""
    app = FastAPI()
    # The REAL problem-body translation, so these assert on the status a caller actually receives
    # rather than on an exception type the framework would have converted anyway. The estate's rule is
    # that endpoints raise typed lance_namespace errors and one shared handler maps them; a test that
    # bypasses the handler is testing a different contract from the one that ships.
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.oidc = oidc
    app.state.fga = fga
    app.state.sa_oidc = sa_oidc

    def _settings() -> IngestAuthSettings:
        s = IngestAuthSettings()
        s.service_project = service_project
        return s

    # Override the REAL dependency rather than declaring a parallel one. `AuthSettingsDep` is what
    # ships; a test that declares its own `Annotated[..., Depends(local_fn)]` is exercising a
    # different wiring from the endpoint's — and did, resolving `settings` as a QUERY parameter and
    # 422-ing before the door ever ran.
    app.dependency_overrides[get_auth_settings] = _settings

    @app.post("/ingests")
    async def create(
        request: Request,
        body: dict[str, Any],
        settings: AuthSettingsDep,
        authorization: Annotated[str | None, Header()] = None,
        dapr_caller_app_id: Annotated[str | None, Header()] = None,
    ):
        await authorize_ingest(request, settings, body.get("project"), authorization, dapr_caller_app_id)
        return {"ok": True}

    return app


class _Verifier:
    def __init__(self, sub: str | None) -> None:
        self._sub = sub
        self.verified: list[str] = []

    def verify(self, raw: str) -> Any:
        self.verified.append(raw)
        if self._sub is None:
            raise UnauthenticatedError("invalid token")
        return type("Tok", (), {"sub": self._sub})()


# ── the service door ──────────────────────────────────────────────────────────────────


def test_an_ANONYMOUS_request_is_refused(service_door: ServiceAccountVerifier) -> None:
    """The hole, closed. Before this, an unauthenticated POST drove a write into any project."""
    with TestClient(_app(sa_oidc=service_door), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}).status_code == 403


def test_a_SERVICE_caller_may_ingest_into_its_CONFIGURED_project(service_door: ServiceAccountVerifier, sa_issuer: Any) -> None:
    """Service-to-service ingest, the credential's actual job: a mapped account, arriving from a service sidecar."""
    with TestClient(_app(sa_oidc=service_door, service_project="demo")) as client:
        response = client.post("/ingests", json={"project": "demo"}, headers={**_service_bearer(sa_issuer), "dapr-caller-app-id": "medallion"})

    assert response.status_code == 200


def test_a_SERVICE_caller_may_NOT_cross_into_another_project(service_door: ServiceAccountVerifier, sa_issuer: Any) -> None:
    """The escalation the per-project check exists to stop.

    A service caller is held to the configured project. Honouring an arbitrary requested project would let one
    service write into every tenant's bronze. Crossing tenants takes a user bearer, which gets the per-project FGA
    check.
    """
    with TestClient(_app(sa_oidc=service_door, service_project="demo"), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "victim"}, headers=_service_bearer(sa_issuer)).status_code == 403


def test_a_service_account_the_door_does_not_map_is_401_and_never_reaches_the_human_door(
    service_door: ServiceAccountVerifier, sa_issuer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signature and audience accept every account in the cluster that carries the audience (P5.3 c0), so the subject
    map is the only binder; a token the cluster issuer claims is answered by that verifier alone."""
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://issuer.test")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")
    human_door = _Verifier("anyone")

    with TestClient(_app(oidc=human_door, fga=object(), sa_oidc=service_door), raise_server_exceptions=False) as client:
        response = client.post("/ingests", json={"project": "demo"}, headers=_service_bearer(sa_issuer, account="rask-sa-notifications"))

    assert response.status_code == 401
    assert human_door.verified == []


def test_a_VALID_service_token_from_the_PUBLIC_DOOR_is_refused(service_door: ServiceAccountVerifier, sa_issuer: Any) -> None:
    """A machine credential arriving through the gateway's sidecar names the proxy's hop, not a service this door
    may trust: the estate's list of public front doors is refused on the service branch."""
    with TestClient(_app(sa_oidc=service_door), raise_server_exceptions=False) as client:
        response = client.post("/ingests", json={"project": "demo"}, headers={**_service_bearer(sa_issuer), "dapr-caller-app-id": "gateway"})

    assert response.status_code == 403, "a service credential relayed by a public front door authorized a write"
    assert "public front door" in response.json()["detail"]


def test_NOTHING_configured_is_dev_open() -> None:
    """Matches every other door in the estate: a dev stack with nothing configured still works.

    Pinned as a TEST so the behaviour is a decision on record rather than an accident — and so that
    tightening it later is a visible change to this assertion, not a silent one.
    """
    with TestClient(_app()) as client:
        assert client.post("/ingests", json={"project": "anything"}).status_code == 200


# ── the human door ────────────────────────────────────────────────────────────────────


@pytest.fixture
def _oidc_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://issuer.test")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")


def _patch_check(monkeypatch: pytest.MonkeyPatch, *, allow: bool, outage: bool = False) -> list[dict[str, str]]:
    asked: list[dict[str, str]] = []

    async def _check(client: object, *, user: str, relation: str, obj: str) -> bool:
        asked.append({"user": user, "relation": relation, "obj": obj})
        if outage:
            raise ServiceUnavailableError("fga down")
        return allow

    monkeypatch.setattr("ingest.auth.fga.check", _check)
    return asked


def test_an_ADMIN_bearer_is_allowed_and_the_check_targets_the_REQUESTED_project(_oidc_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Authorization scope must equal WRITE scope.

    Checking a fixed configured project instead would let an admin of project A pass the gate while
    the rows land in project B — the gate would be real and pointed at the wrong thing.
    """
    asked = _patch_check(monkeypatch, allow=True)

    with TestClient(_app(oidc=_Verifier("alice"), fga=object())) as client:
        r = client.post("/ingests", json={"project": "tenant-b"}, headers={"authorization": "Bearer t"})

    assert r.status_code == 200
    assert asked == [{"user": "alice", "relation": "can_administer", "obj": "project:tenant-b"}]


def test_a_NON_admin_bearer_is_refused(_oidc_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_check(monkeypatch, allow=False)

    with TestClient(_app(oidc=_Verifier("mallory"), fga=object()), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "tenant-b"}, headers={"authorization": "Bearer t"}).status_code == 403


def test_an_INVALID_bearer_is_401_not_403(_oidc_on: None) -> None:
    """A caller with a bad token has an authentication problem, not a permission one — and telling
    them "forbidden" sends them looking for a grant they do not need."""
    with TestClient(_app(oidc=_Verifier(None), fga=object()), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}, headers={"authorization": "Bearer bad"}).status_code == 401


def test_an_FGA_OUTAGE_is_503_never_an_allow(_oidc_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The most important negative in the file.

    An unreachable authz service must not become a silent allow, and must not become a 403 either:
    a 403 tells an admin they lack a permission they hold, and hides an incident behind a status
    nobody pages on.
    """
    _patch_check(monkeypatch, allow=True, outage=True)

    with TestClient(_app(oidc=_Verifier("alice"), fga=object()), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}, headers={"authorization": "Bearer t"}).status_code == 503


def test_OIDC_on_with_NO_verifier_is_503_not_a_denial(_oidc_on: None) -> None:
    """Startup/discovery skew is an infrastructure fault, not a caller verdict."""
    with TestClient(_app(oidc=None, fga=object()), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}, headers={"authorization": "Bearer t"}).status_code == 503


def test_OIDC_on_with_NO_fga_client_fails_CLOSED(_oidc_on: None) -> None:
    """Authenticated is not authorized. With no checker wired the door must refuse, never wave through
    a verified-but-unchecked principal."""
    with TestClient(_app(oidc=_Verifier("alice"), fga=None), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}, headers={"authorization": "Bearer t"}).status_code == 503


# ── ING-01: an absent service door is not an absent door ─────────────────────────────────────────


def test_with_OIDC_on_and_no_service_door_an_anonymous_request_is_refused(_oidc_on: None) -> None:
    """The open posture means NOTHING is configured to authenticate against; a deployment with no service-account
    issuer but OIDC on is not open (docs/DECISIONS.md "The Python estate audit", ING-01)."""
    with TestClient(_app(fga=object()), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}).status_code == 403


# ── ING-12 / ING-13: the signature reads as what it is, and settings are built once ──────


def test_service_project_override_reads_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ING-13: folding the `RASK_INGEST_SERVICE_PROJECT` override into the model must not lose it."""
    from ingest import auth

    monkeypatch.setenv("RASK_INGEST_SERVICE_PROJECT", "acme")
    if hasattr(auth.get_auth_settings, "cache_clear"):
        auth.get_auth_settings.cache_clear()

    try:
        assert auth.get_auth_settings().service_project == "acme"
    finally:
        if hasattr(auth.get_auth_settings, "cache_clear"):
            auth.get_auth_settings.cache_clear()
