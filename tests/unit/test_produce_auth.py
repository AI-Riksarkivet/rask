"""Fail-closed dual-auth for the /produce + /train triggers (#64): service token OR project-admin OIDC.

The cascade head is provenance-fabricatable, so the ADMIN door added for the UI must not be bypassable:
an invalid bearer 401s, a non-admin 403s, an FGA outage 503s (never a silent allow), and a request carrying
no credential 403s. The service-token path is UNCHANGED, and dev (no APP_API_TOKEN) stays open.

Two layers: direct-function tests pin every fail-closed branch of :func:`authorize_produce` (sync via
``asyncio.run`` — no async-plugin dependency); the TestClient tests pin that it is actually WIRED onto the
``/produce`` route (a direct, non-sidecar POST is gated end-to-end), which the function tests can't prove.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError, UnauthenticatedError
from medallion.api import produce_auth
from medallion.api.dependencies import get_dapr, get_settings
from medallion.api.produce import router
from medallion.api.train import router as train_router
from medallion.core.config import MedallionSettings
from openfga_sdk import OpenFgaClient

from service_kit.governed.audit import AUDIT_LOGGER, configure_audit


# ── direct-function tests: every fail-closed branch of authorize_produce ──────────────────────────


class _Verifier:
    def __init__(self, sub: str = "alice", *, invalid: bool = False) -> None:
        self._sub, self._invalid = sub, invalid

    def verify(self, _token: str) -> object:  # the fake ignores the token
        if self._invalid:
            raise UnauthenticatedError("bad token")
        return SimpleNamespace(sub=self._sub)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_token: str | None,
    dapr_token: str | None = None,
    authz: str | None = None,
    verifier: object | None = None,
    oidc_enabled: bool = True,
    fga_result: bool = True,
    fga_raises: bool = False,
    project: str | None = None,
    captured: dict[str, object] | None = None,
) -> None:
    if app_token is None:
        monkeypatch.delenv("APP_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("APP_API_TOKEN", app_token)

    async def fake_check(_client: object, **kw: object) -> bool:  # user=/relation=/obj= arrive as kwargs
        if captured is not None:
            captured.update(kw)
        if fga_raises:
            raise ServiceUnavailableError("fga down")
        return fga_result

    monkeypatch.setattr(produce_auth.fga, "check", fake_check)
    ns = SimpleNamespace(oidc_enabled=oidc_enabled, produce_admin_project="acme")
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=verifier))))
    settings = cast(MedallionSettings, ns)
    return asyncio.run(
        produce_auth.authorize_produce(
            request,
            settings,
            cast(OpenFgaClient, object()),
            dapr_api_token=dapr_token,
            authorization=authz,
            project=project,
        )
    )


def _expect(monkeypatch: pytest.MonkeyPatch, status: int, **kw: object) -> None:
    with pytest.raises(HTTPException) as exc:
        _run(monkeypatch, **kw)  # ty: ignore[invalid-argument-type]
    assert exc.value.status_code == status


def test_dev_open_when_no_service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, app_token=None) is None  # dev no-op, exactly like require_dapr_token


def test_service_token_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, app_token="s3cr3t", dapr_token="s3cr3t") is None


def test_oidc_admin_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), fga_result=True) is None


def test_oidc_nonadmin_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 403, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), fga_result=False)


def test_invalid_bearer_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 401, app_token="s3cr3t", authz="Bearer bad", verifier=_Verifier(invalid=True))


def test_malformed_authorization_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 401, app_token="s3cr3t", authz="Basic xyz", verifier=_Verifier())


def test_fga_outage_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 503, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), fga_raises=True)


def test_no_credential_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 403, app_token="s3cr3t")  # token set, no dapr token, no bearer, no verifier


def test_bearer_but_oidc_disabled_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bearer is presented but OIDC is off → the human door is shut; never a silent allow.
    _expect(monkeypatch, 403, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), oidc_enabled=False)


def test_bearer_but_unwired_verifier_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # OIDC enabled but app.state.oidc was never wired (startup/discovery skew): a bearer-presenting caller
    # must surface the auth-layer OUTAGE (503, the catalog/lineage security.py invariant), never the
    # terminal 403 — a valid admin would otherwise be misreported as denied, and 503-keyed monitoring
    # (which the FGA-unwired branch already feeds) would miss the misconfiguration.
    _expect(monkeypatch, 503, app_token="s3cr3t", authz="Bearer good", verifier=None)


def test_wrong_service_token_and_oidc_off_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 403, app_token="s3cr3t", dapr_token="wrong", oidc_enabled=False)


# ── route-wiring tests: authorize_produce is actually mounted on POST /produce ────────────────────


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    app = FastAPI()
    app.include_router(router)
    # Fakes so only the guard is exercised — a rejected request never reaches the handler anyway. Settings
    # carries oidc_enabled=False (no verifier wired on app.state) so the human door stays shut in the test.
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(oidc_enabled=False, produce_admin_project="acme")
    return TestClient(app, raise_server_exceptions=False)


def test_route_rejects_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _client(monkeypatch).post("/produce").status_code == 403


def test_route_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _client(monkeypatch).post("/produce", headers={"dapr-api-token": "nope"}).status_code == 403


def test_route_token_match_passes_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # Correct token → the guard passes; the handler then runs against the fakes (may 5xx) but is NOT a 403.
    response = _client(monkeypatch).post("/produce", headers={"dapr-api-token": "s3cret"})
    assert response.status_code != 403


# ── GET /authorize (#77 audit admin gate): the SAME door, side-effect-free ─────────────────────────


def test_authorize_route_rejects_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    # The web audit BFF relies on this: a non-admin (no credential) must be refused, never 200.
    assert _client(monkeypatch).get("/authorize").status_code == 403


def test_authorize_route_allows_the_admin_door(monkeypatch: pytest.MonkeyPatch) -> None:
    res = _client(monkeypatch).get("/authorize", headers={"dapr-api-token": "s3cret"})
    assert res.status_code == 200 and res.json() == {"authorized": True}


# ── #84 per-tenant produce: the admin gate follows the REQUESTED project ───────────────────────────


def test_oidc_admin_gate_targets_the_requested_project(monkeypatch: pytest.MonkeyPatch) -> None:
    # A caller producing into project X must administer X — not the fixed configured project.
    captured: dict[str, object] = {}
    _run(
        monkeypatch,
        app_token="s3cr3t",
        authz="Bearer good",
        verifier=_Verifier(),
        project="globex",
        captured=captured,
    )
    assert captured["obj"] == "project:globex"


def test_oidc_admin_gate_defaults_to_the_configured_project(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    _run(monkeypatch, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), captured=captured)
    assert captured["obj"] == "project:acme"  # no project param → exactly the pre-#84 gate


def test_service_token_with_the_configured_project_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    # The service path stays open for the project it is configured to produce into.
    assert _run(monkeypatch, app_token="s3cr3t", dapr_token="s3cr3t", project="acme") is None


def test_service_token_cannot_request_another_project(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared app token authenticates the SERVICE, not a tenant — trusting it for an arbitrary
    # requested project would let any token holder produce into every tenant. Cross-project requests
    # take a user bearer — the per-project FGA check test_oidc_admin_gate_targets_the_requested_project pins.
    _expect(monkeypatch, 403, app_token="s3cr3t", dapr_token="s3cr3t", project="globex")


def test_nonadmin_of_the_requested_project_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(
        monkeypatch,
        403,
        app_token="s3cr3t",
        authz="Bearer good",
        verifier=_Verifier(),
        fga_result=False,
        project="globex",
    )


def test_route_rejects_a_malformed_project_with_422(monkeypatch: pytest.MonkeyPatch) -> None:
    # The project becomes an S3 prefix + lineage qualifier — a path-shaped value is refused at the edge.
    res = _client(monkeypatch).get("/authorize", params={"project": "../evil"}, headers={"dapr-api-token": "s3cret"})
    assert res.status_code == 422


def test_produce_route_409s_when_project_routing_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dev-open door + real settings (control_root unset): a project-carrying produce is REFUSED (409,
    # problem+json), never silently seeded into the shared root.
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: MedallionSettings.model_validate({})
    res = TestClient(app, raise_server_exceptions=False).post("/produce", params={"project": "acme"})
    assert res.status_code == 409
    assert res.headers["content-type"].startswith("application/problem+json")


# ── /train gate: pinned to the CONFIGURED project — a caller-supplied ?project= is ignored ─────────


def test_train_gate_declares_no_project_param() -> None:
    # The pin is structural: authorize_train has NO `project` parameter, so FastAPI never binds a
    # caller's ?project= into the train gate — training writes single-tenant state under the configured
    # produce_admin_project, and authorization scope must equal write scope.
    assert "project" not in inspect.signature(produce_auth.authorize_train).parameters


def test_train_gate_checks_the_configured_project(monkeypatch: pytest.MonkeyPatch) -> None:
    # The OIDC admin door through authorize_train always targets the CONFIGURED project.
    monkeypatch.setenv("APP_API_TOKEN", "s3cr3t")
    captured: dict[str, object] = {}

    async def fake_check(_client: object, **kw: object) -> bool:
        captured.update(kw)
        return True

    monkeypatch.setattr(produce_auth.fga, "check", fake_check)
    ns = SimpleNamespace(oidc_enabled=True, produce_admin_project="acme")
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=_Verifier()))))
    asyncio.run(
        produce_auth.authorize_train(
            request,
            cast(MedallionSettings, ns),
            cast(OpenFgaClient, object()),
            dapr_api_token=None,
            authorization="Bearer good",
        )
    )
    assert captured["obj"] == "project:acme"


def _train_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    app = FastAPI()
    app.include_router(train_router)
    app.include_router(router)  # /produce mounted alongside, to contrast the per-project behavior
    app.dependency_overrides[get_dapr] = lambda: None
    # ray_enabled=False → a request PASSING the guard hits the disabled-head 409 (a crisp "guard passed"
    # signal distinct from the guard's own 403); oidc off keeps the human door shut.
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        oidc_enabled=False, produce_admin_project="acme", ray_enabled=False, s3_endpoint="", raw_uri=""
    )
    return TestClient(app, raise_server_exceptions=False)


_TRAIN_BODY = {"model": "m1", "features": [{"dataset": "silver$feats"}]}


def test_train_route_ignores_a_caller_supplied_project(monkeypatch: pytest.MonkeyPatch) -> None:
    # Service token + ?project=other on /train: the stray param is IGNORED — the guard passes (pinned to
    # the configured project) and the request proceeds to the handler (here the disabled-head 409).
    res = _train_client(monkeypatch).post("/train", params={"project": "globex"}, json=_TRAIN_BODY, headers={"dapr-api-token": "s3cret"})
    assert res.status_code == 409, res.text


def test_produce_route_keeps_the_per_project_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    # …while the SAME credential + ?project=other on /produce keeps the per-project behavior: the shared
    # service token carries no tenant identity, so a cross-project produce stays 403.
    res = _train_client(monkeypatch).post("/produce", params={"project": "globex"}, headers={"dapr-api-token": "s3cret"})
    assert res.status_code == 403, res.text


# ── audit (#41): every door decision lands on lance.audit — ALLOW/DENY/FAILURE, service path too ───


class _CaptureAudit(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def audit_records() -> Iterator[list[logging.LogRecord]]:
    """Capture the dedicated ``lance.audit`` stream with the trail enabled (as the producer boot does)."""
    handler = _CaptureAudit()
    logger = logging.getLogger(AUDIT_LOGGER)
    configure_audit(enabled=True)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        configure_audit(enabled=True)  # leave the stream on for the rest of the suite


def _audit_fields(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in record.__dict__.items() if k.startswith("audit.")}


def test_admin_allow_is_audited(monkeypatch: pytest.MonkeyPatch, audit_records: list[logging.LogRecord]) -> None:
    # The cascade-head trigger is exactly what the #77 audit viewer reviews — the allowed decision must
    # land with who/what/resource, like every catalog can_administer decision (fga_deps._require parity).
    _run(monkeypatch, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), fga_result=True)
    assert len(audit_records) == 1
    assert _audit_fields(audit_records[0]) == {
        "audit.action": "can_administer",
        "audit.outcome": "allow",
        "audit.subject": "alice",
        "audit.resource": "project:acme",
    }


def test_admin_deny_is_audited(monkeypatch: pytest.MonkeyPatch, audit_records: list[logging.LogRecord]) -> None:
    _expect(monkeypatch, 403, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), fga_result=False)
    fields = _audit_fields(audit_records[0])
    assert fields["audit.action"] == "can_administer" and fields["audit.outcome"] == "deny"


def test_fga_outage_is_audited_as_failure(monkeypatch: pytest.MonkeyPatch, audit_records: list[logging.LogRecord]) -> None:
    _expect(monkeypatch, 503, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), fga_raises=True)
    fields = _audit_fields(audit_records[0])
    assert fields["audit.outcome"] == "failure" and fields["audit.reason"] == "authz_unavailable"


def test_service_token_acceptance_is_audited(monkeypatch: pytest.MonkeyPatch, audit_records: list[logging.LogRecord]) -> None:
    # The service path opens the same door, so its acceptance is recorded too; the shared token names no
    # principal, hence the fixed "service" subject.
    _run(monkeypatch, app_token="s3cr3t", dapr_token="s3cr3t")
    assert _audit_fields(audit_records[0]) == {
        "audit.action": "produce_service_token",
        "audit.outcome": "allow",
        "audit.subject": "service",
        "audit.resource": "project:acme",
    }


def test_service_token_cross_project_refusal_is_audited(monkeypatch: pytest.MonkeyPatch, audit_records: list[logging.LogRecord]) -> None:
    _expect(monkeypatch, 403, app_token="s3cr3t", dapr_token="s3cr3t", project="globex")
    fields = _audit_fields(audit_records[0])
    assert fields["audit.outcome"] == "deny" and fields["audit.reason"] == "cross_project"
    assert fields["audit.resource"] == "project:globex"


def test_medallion_audit_stream_is_env_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    # The producer boot gates `lance.audit` on the SHARED LANCE_AUDIT_ENABLED (catalog parity — one flag
    # for the estate's compliance posture): default on, and the env alias turns the stream off.
    assert MedallionSettings.model_validate({}).audit_enabled is True
    monkeypatch.setenv("LANCE_AUDIT_ENABLED", "false")
    assert MedallionSettings().audit_enabled is False
