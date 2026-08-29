"""The ingest control API is not a public door.

It shipped with NO authentication. That is worse than an ordinary missing gate, because the endpoint
takes a caller-supplied SOURCE: with `local-dir` it was one anonymous request to read the ingest pod's
own filesystem into a governed table (the confinement in `adapters.py` closed that primitive), and
with any kind it is an anonymous writer into a tenant's bronze tier and an anonymous READER of run
records that name a project, its datasets, its source keys and its errors.

Both doors are asserted here, and both fail closed. The shape is deliberately the medallion's
`authorize_produce`: same question, so it must not have a second, weaker answer.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import pytest
from fastapi import FastAPI, Header, Request
from fastapi.testclient import TestClient
from ingest.auth import AuthSettingsDep, IngestAuthSettings, authorize_ingest, get_auth_settings
from lance_namespace import ServiceUnavailableError, UnauthenticatedError

from service_kit.lakehouse.ns_errors import install_problem_handlers


SERVICE_TOKEN = "s3cr3t-service-token"


def _app(*, oidc: object = None, fga: object = None, service_project: str = "demo") -> FastAPI:
    """A minimal app carrying only what the door reads."""
    app = FastAPI()
    # The REAL problem-body translation, so these assert on the status a caller actually receives
    # rather than on an exception type the framework would have converted anyway. The estate's rule is
    # that endpoints raise typed lance_namespace errors and one shared handler maps them; a test that
    # bypasses the handler is testing a different contract from the one that ships.
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.oidc = oidc
    app.state.fga = fga

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
        dapr_api_token: Annotated[str | None, Header()] = None,
        authorization: Annotated[str | None, Header()] = None,
        dapr_caller_app_id: Annotated[str | None, Header()] = None,
    ):
        await authorize_ingest(request, settings, body.get("project"), dapr_api_token, authorization, dapr_caller_app_id)
        return {"ok": True}

    return app


def test_an_ANONYMOUS_request_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hole, closed. Before this, an unauthenticated POST drove a write into any project."""
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)

    with TestClient(_app(), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}).status_code == 403


def test_a_WRONG_service_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compared with `compare_digest`, so a near-miss is refused in constant time rather than leaking
    a prefix through timing."""
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)

    with TestClient(_app(), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}, headers={"dapr-api-token": SERVICE_TOKEN + "x"}).status_code == 403


def test_the_SERVICE_token_may_ingest_into_its_CONFIGURED_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)

    with TestClient(_app(service_project="demo")) as client:
        assert client.post("/ingests", json={"project": "demo"}, headers={"dapr-api-token": SERVICE_TOKEN}).status_code == 200


def test_the_SERVICE_token_may_NOT_cross_into_another_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escalation the per-project check exists to stop.

    The shared token authenticates a SERVICE, not a tenant. Honouring an arbitrary requested project
    would let any holder of it write into every tenant's bronze — and the token is mounted in several
    pods. Crossing tenants takes a user bearer, which gets the per-project FGA check.
    """
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)

    with TestClient(_app(service_project="demo"), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "victim"}, headers={"dapr-api-token": SERVICE_TOKEN}).status_code == 403


def test_NO_service_token_configured_is_dev_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matches every other door in the estate: a dev stack with nothing configured still works.

    Pinned as a TEST so the behaviour is a decision on record rather than an accident — and so that
    tightening it later is a visible change to this assertion, not a silent one.
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with TestClient(_app()) as client:
        assert client.post("/ingests", json={"project": "anything"}).status_code == 200


# ── the human door ────────────────────────────────────────────────────────────────────


class _Verifier:
    def __init__(self, sub: str | None) -> None:
        self._sub = sub

    def verify(self, raw: str) -> Any:
        if self._sub is None:
            raise UnauthenticatedError("invalid token")
        return type("Tok", (), {"sub": self._sub})()


class _Fga:
    def __init__(self, allow: bool, *, outage: bool = False) -> None:
        self._allow = allow
        self._outage = outage
        self.asked: list[tuple[str, str, str]] = []


@pytest.fixture
def _oidc_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("LANCE_OIDC_ENABLED", "true")
    monkeypatch.setenv("LANCE_OIDC_ISSUER", "https://issuer.test")
    monkeypatch.setenv("LANCE_OIDC_AUDIENCE", "rask")


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


# ── the gateway must not launder anonymous traffic into a service-authenticated write ──


def test_a_VALID_service_token_from_the_PUBLIC_DOOR_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured bypass: 403 straight to the pod, 202 through the gateway, with NO credential.

    `dapr.io/app-token-secret` makes daprd stamp `dapr-api-token` on every request it hands the app,
    and the gateway forwards through Dapr service invocation, so an anonymous public request arrives
    here already holding a valid service token. A browser with no login started a real ingest run
    that way. The token proves the request came through Dapr; it says nothing about who sent it.
    """
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)

    with TestClient(_app(), raise_server_exceptions=False) as client:
        response = client.post(
            "/ingests",
            json={"project": "demo"},
            headers={"dapr-api-token": SERVICE_TOKEN, "dapr-caller-app-id": "gateway"},
        )

    assert response.status_code == 403, "a token stamped by daprd on behalf of an anonymous caller authorized a write"
    assert "public front door" in response.json()["detail"]


def test_the_SAME_token_from_a_SERVICE_caller_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not sever service-to-service ingest, which is the token's actual job.

    Without this, the fix would be indistinguishable from deleting the service-token path — and every
    cascade head that legitimately drives an ingest would start failing.
    """
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)

    with TestClient(_app()) as client:
        response = client.post(
            "/ingests",
            json={"project": "demo"},
            headers={"dapr-api-token": SERVICE_TOKEN, "dapr-caller-app-id": "medallion"},
        )

    assert response.status_code == 200


def test_a_DIRECT_caller_with_no_dapr_hop_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """No caller app-id means nothing invoked through Dapr — the caller PRESENTED the token itself.

    That is the conformance lane's path (it reads APP_API_TOKEN from the pod's own env), and holding
    the shared secret is the credential there. Refusing it would break the lane while closing
    nothing: a party with the secret AND network reach to the pod never needed the gateway.
    """
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)

    with TestClient(_app()) as client:
        assert client.post("/ingests", json={"project": "demo"}, headers={"dapr-api-token": SERVICE_TOKEN}).status_code == 200


# ── ING-01: an absent service token is not an absent door ─────────────────────────────────────────
#
# Every test above SETS `APP_API_TOKEN`, which is exactly how the bypass survived a read-through audit.
# `authorize_ingest` opened with `if not expected: return`, so a deployment with OIDC enabled, a live
# FGA client and a BLANK or unset service token accepted every ingest from anyone who could reach the
# port — while `/v1/me`, the navbar and the catalog all reported authorization as ON. The blank case is
# the likelier one: a secret that renders empty is far more common than one nobody wired, and it failed
# open rather than loudly.


@pytest.fixture
def _oidc_on_without_service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_oidc_on` minus the service token — the deployment shape the bypass lived in."""
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    monkeypatch.setenv("LANCE_OIDC_ENABLED", "true")
    monkeypatch.setenv("LANCE_OIDC_ISSUER", "https://issuer.test")
    monkeypatch.setenv("LANCE_OIDC_AUDIENCE", "rask")


@pytest.mark.usefixtures("_oidc_on_without_service_token")
def test_an_unset_service_token_does_not_open_the_door_when_auth_is_ON() -> None:
    """ING-01. The SERVICE door is absent; the USER door must still be shut."""
    with TestClient(_app(fga=object()), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}).status_code == 403


@pytest.mark.usefixtures("_oidc_on_without_service_token")
def test_a_stray_service_token_header_cannot_authenticate_when_none_is_configured() -> None:
    """With no `APP_API_TOKEN`, any `dapr-api-token` a caller invents must authenticate nothing.

    Also guards the `expected and …` ordering: comparing against `None` would raise and answer 500,
    which reads as a broken service rather than as a refused credential.
    """
    with TestClient(_app(fga=object()), raise_server_exceptions=False) as client:
        response = client.post("/ingests", json={"project": "demo"}, headers={"dapr-api-token": "anything-at-all"})

    assert response.status_code == 403


def test_a_BLANK_service_token_does_not_open_the_door_when_auth_is_ON(monkeypatch: pytest.MonkeyPatch) -> None:
    """The likelier shape of the same fault — a secret that rendered empty, not one nobody wired."""
    monkeypatch.setenv("APP_API_TOKEN", "")
    monkeypatch.setenv("LANCE_OIDC_ENABLED", "true")
    monkeypatch.setenv("LANCE_OIDC_ISSUER", "https://issuer.test")
    monkeypatch.setenv("LANCE_OIDC_AUDIENCE", "rask")

    with TestClient(_app(fga=object()), raise_server_exceptions=False) as client:
        assert client.post("/ingests", json={"project": "demo"}).status_code == 403


def test_the_local_dev_posture_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing configured to authenticate against: the documented open stack stays open.

    The half that keeps the fix honest. Closing the door unconditionally would break `make dev-micro`
    and every local ingest — and a fix that forces everyone to run OIDC locally is a fix nobody keeps.
    """
    monkeypatch.delenv("APP_API_TOKEN", raising=False)

    with TestClient(_app()) as client:
        assert client.post("/ingests", json={"project": "demo"}).status_code == 200


# ── ING-12 / ING-13: the signature reads as what it is, and settings are built once ──────


def test_authorize_ingest_is_not_dependency_shaped() -> None:
    """ING-12: `authorize_ingest` is invoked positionally at every call site, never `Depends()`d.

    Its parameters therefore must not carry FastAPI `Header()`/`Depends()` bindings — those are inert
    here (the ROUTE params carry the real header binding and pass the values in) and read as though
    this were a dependency, which is the one thing it is not. The real risk of the misread is the
    tempting "wire it in as a dependency" fix: as a dependency its `project` would bind as a query
    param defaulting to None, silently scoping the admin check to the configured project instead of
    the body's — a real cross-project regression. So the signature stays plain.
    """
    import inspect
    import typing

    from fastapi import params as fastapi_params

    binding = (fastapi_params.Header, fastapi_params.Depends)
    hints = typing.get_type_hints(authorize_ingest, include_extras=True)
    annotated = [name for name, hint in hints.items() if any(isinstance(meta, binding) for meta in getattr(hint, "__metadata__", ()))]
    defaulted = [name for name, param in inspect.signature(authorize_ingest).parameters.items() if isinstance(param.default, binding)]

    offenders = sorted(set(annotated + defaulted))
    assert not offenders, f"authorize_ingest carries inert FastAPI dependency annotations on {offenders}"


def test_get_auth_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """ING-13: the settings are constructed once, not rebuilt (and `.env` re-read from disk) per request.

    `AuthSettingsDep` resolves `get_auth_settings` on every governed route; an uncached factory
    re-instantiates the `BaseSettings` — a disk read of `.env` — on each one.
    """
    from ingest import auth

    calls = 0
    original_init = auth.IngestAuthSettings.__init__

    # `Any`, not `object`: this wrapper forwards verbatim into pydantic-settings' typed `__init__`,
    # and `object` would fail every one of its keyword parameters at the forwarding call.
    def _counting_init(self: auth.IngestAuthSettings, *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(auth.IngestAuthSettings, "__init__", _counting_init)
    # Clear whatever earlier tests cached, so the count reflects only this test's two calls. The guard
    # is what makes the RED legible: an uncached factory has no `cache_clear`, and the point is the two
    # calls below then construct twice.
    if hasattr(auth.get_auth_settings, "cache_clear"):
        auth.get_auth_settings.cache_clear()

    auth.get_auth_settings()
    auth.get_auth_settings()

    assert calls == 1, f"IngestAuthSettings was constructed {calls} times across two calls — get_auth_settings is not cached"


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
