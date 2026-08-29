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
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespaceError, ServiceUnavailableError, UnauthenticatedError
from medallion.api import produce_auth
from medallion.api.dependencies import get_dapr, get_settings
from medallion.api.produce import router
from medallion.api.train import router as train_router
from medallion.core.config import MedallionSettings
from openfga_sdk import OpenFgaClient

from service_kit.governed.audit import AUDIT_LOGGER, configure_audit
from service_kit.lakehouse.ns_errors import install_problem_handlers, status_for


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
    caller_app_id: str | None = None,
    captured: dict[str, object] | None = None,
) -> str | None:
    async def fake_check(_client: object, **kw: object) -> bool:  # user=/relation=/obj= arrive as kwargs
        if captured is not None:
            captured.update(kw)
        if fga_raises:
            raise ServiceUnavailableError("fga down")
        return fga_result

    monkeypatch.setattr(produce_auth.fga, "check", fake_check)
    # The token rides SETTINGS (MED-009): the door reads `settings.app_api_token`, never the raw env,
    # so the fake carries the field — `None` maps to the field's unset default ("", the dev-open case).
    ns = SimpleNamespace(oidc_enabled=oidc_enabled, produce_admin_project="acme", app_api_token=app_token or "")
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
            dapr_caller_app_id=caller_app_id,
        )
    )


def _expect(monkeypatch: pytest.MonkeyPatch, status: int, **kw: object) -> None:
    # The gate raises the lance_namespace domain errors (same taxonomy as catalog/lineage security), so the
    # HTTP status is the ns_errors mapping of the error code, not an HTTPException attribute.
    with pytest.raises(LanceNamespaceError) as exc:
        _run(monkeypatch, **kw)  # ty: ignore[invalid-argument-type]
    assert status_for(int(exc.value.code)) == status


def test_dev_open_when_no_service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, app_token=None) is None  # dev no-op, exactly like require_dapr_token


def test_service_token_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, app_token="s3cr3t", dapr_token="s3cr3t") is None


def test_oidc_admin_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allowed = returns without raising. It used to assert `is None` because the gate returned nothing;
    the subject it now hands back is the cascade's originator, and NOT raising is still the whole claim."""
    assert _run(monkeypatch, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), fga_result=True) == "alice"


def test_oidc_nonadmin_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 403, app_token="s3cr3t", authz="Bearer good", verifier=_Verifier(), fga_result=False)


def test_invalid_bearer_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 401, app_token="s3cr3t", authz="Bearer bad", verifier=_Verifier(invalid=True))


def test_malformed_authorization_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 401, app_token="s3cr3t", authz="Basic xyz", verifier=_Verifier())


# ── the bearer is verified OFF the event loop (open_python-audit ING-02, on this door) ─────────────


class _ThreadRecordingVerifier:
    """Records which thread ``verify`` ran on.

    Asserting "off the loop" by timing is a flake waiting to happen. Thread identity is exact: the
    coroutine runs on the thread that called ``asyncio.run``, so a verify that lands on THAT thread
    ran inline on the loop, and one that lands anywhere else was handed to a worker.
    """

    def __init__(self) -> None:
        self.thread: int | None = None

    def verify(self, _token: str) -> object:
        self.thread = threading.get_ident()
        return SimpleNamespace(sub="alice")


def test_the_produce_door_verifies_the_bearer_OFF_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OIDCVerifier.verify`` does synchronous discovery + JWKS fetches — never on the loop.

    The cascade head's write doors all funnel through this dependency, and ``service_kit.probes`` is
    mounted on the same app, so an inline verify on a cold cache or a key rotation stalls every
    in-flight request in the pod AND its own liveness probe. The identical defect was filed and fixed
    on the sibling ingest door (ING-02); it did not travel here because the two doors are copies.
    """
    verifier = _ThreadRecordingVerifier()
    assert _run(monkeypatch, app_token="s3cr3t", authz="Bearer good", verifier=verifier) == "alice"
    assert verifier.thread is not None, "verify() was never called — the test proves nothing"
    assert verifier.thread != threading.get_ident(), "verify() ran on the event loop thread"


def test_the_promotion_door_verifies_the_bearer_OFF_the_event_loop() -> None:
    """Same rule for ``authenticate_subject`` — the promotion review's door, and the second copy.

    It is a separate function with its own ``verifier.verify`` call, so fixing ``authorize_produce``
    alone would leave the promotion approve/reject routes stalling the loop.
    """
    verifier = _ThreadRecordingVerifier()
    ns = SimpleNamespace(oidc_enabled=True, produce_admin_project="acme")
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=verifier))))
    sub = asyncio.run(produce_auth.authenticate_subject(request, cast(MedallionSettings, ns), authorization="Bearer good"))
    assert sub == "alice"
    assert verifier.thread is not None, "verify() was never called — the test proves nothing"
    assert verifier.thread != threading.get_ident(), "verify() ran on the event loop thread"


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


def _client() -> TestClient:
    app = FastAPI()
    # The synthetic app installs the SAME problem+json handlers the producer does, so the guard's
    # domain errors map to their HTTP statuses here exactly as in production (they are lance_namespace
    # errors, not HTTPExceptions — a bare FastAPI() would surface them as 500).
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(router)
    # Fakes so only the guard is exercised — a rejected request never reaches the handler anyway. Settings
    # carries oidc_enabled=False (no verifier wired on app.state) so the human door stays shut in the test.
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(oidc_enabled=False, produce_admin_project="acme", app_api_token="s3cret")
    return TestClient(app, raise_server_exceptions=False)


def test_route_rejects_missing_token() -> None:
    assert _client().post("/produce").status_code == 403


def test_route_rejects_wrong_token() -> None:
    assert _client().post("/produce", headers={"dapr-api-token": "nope"}).status_code == 403


def test_route_token_match_passes_the_guard() -> None:
    # Correct token → the guard passes; the handler then runs against the fakes (may 5xx) but is NOT a 403.
    response = _client().post("/produce", headers={"dapr-api-token": "s3cret"})
    assert response.status_code != 403


# ── GET /authorize (#77 audit admin gate): the SAME door, side-effect-free ─────────────────────────


def test_authorize_route_rejects_missing_credential() -> None:
    # The web audit BFF relies on this: a non-admin (no credential) must be refused, never 200.
    assert _client().get("/authorize").status_code == 403


def test_authorize_route_allows_the_admin_door() -> None:
    res = _client().get("/authorize", headers={"dapr-api-token": "s3cret"})
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


def test_route_rejects_a_malformed_project_with_422() -> None:
    # The project becomes an S3 prefix + lineage qualifier — a path-shaped value is refused at the edge.
    res = _client().get("/authorize", params={"project": "../evil"}, headers={"dapr-api-token": "s3cret"})
    assert res.status_code == 422


def test_produce_route_409s_when_project_routing_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dev-open door + real settings (control_root unset): a project-carrying produce is REFUSED (409,
    # problem+json), never silently seeded into the shared root.
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: MedallionSettings.model_validate({})
    res = TestClient(app, raise_server_exceptions=False).post("/produce", params={"project": "acme"}, headers={"Idempotency-Key": "idem-test"})
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
    captured: dict[str, object] = {}

    async def fake_check(_client: object, **kw: object) -> bool:
        captured.update(kw)
        return True

    monkeypatch.setattr(produce_auth.fga, "check", fake_check)
    ns = SimpleNamespace(oidc_enabled=True, produce_admin_project="acme", app_api_token="s3cr3t")
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


def _train_client() -> TestClient:
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(train_router)
    app.include_router(router)  # /produce mounted alongside, to contrast the per-project behavior
    app.dependency_overrides[get_dapr] = lambda: None
    # ray_enabled=False → a request PASSING the guard hits the disabled-head 409 (a crisp "guard passed"
    # signal distinct from the guard's own 403); oidc off keeps the human door shut.
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        oidc_enabled=False, produce_admin_project="acme", app_api_token="s3cret", ray_enabled=False, s3_endpoint="", bronze_uri=""
    )
    return TestClient(app, raise_server_exceptions=False)


_TRAIN_BODY = {"model": "m1", "features": [{"dataset": "silver$feats"}]}


def test_train_route_ignores_a_caller_supplied_project() -> None:
    # Service token + ?project=other on /train: the stray param is IGNORED — the guard passes (pinned to
    # the configured project) and the request proceeds to the handler (here the disabled-head 409).
    res = _train_client().post("/train", params={"project": "globex"}, json=_TRAIN_BODY, headers={"dapr-api-token": "s3cret", "Idempotency-Key": "idem-test"})
    assert res.status_code == 409, res.text


def test_produce_route_keeps_the_per_project_refusal() -> None:
    # …while the SAME credential + ?project=other on /produce keeps the per-project behavior: the shared
    # service token carries no tenant identity, so a cross-project produce stays 403.
    res = _train_client().post("/produce", params={"project": "globex"}, headers={"dapr-api-token": "s3cret"})
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
    # The service path opens the same door, so its acceptance is recorded too. The subject now names
    # WHICH caller — the shared token names no principal, but the Dapr caller app-id does, and an
    # audit line reading only "service" cannot answer "which one", which is the question an incident
    # starts from. `direct` marks a caller that reached the app without a Dapr invocation hop (Service
    # DNS or the pod itself), which is a materially different fact from "some service".
    _run(monkeypatch, app_token="s3cr3t", dapr_token="s3cr3t")
    assert _audit_fields(audit_records[0]) == {
        "audit.action": "produce_service_token",
        "audit.outcome": "allow",
        "audit.subject": "service:direct",
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


# ── the gateway must not launder anonymous traffic into a governed write ──────


def test_a_VALID_service_token_from_the_PUBLIC_DOOR_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured bypass, on the highest-value door in the estate.

    `dapr.io/app-token-secret` makes daprd stamp `dapr-api-token` on every request it hands the app,
    and the gateway forwards `/api/produce` through Dapr service invocation — so an anonymous public
    request reaches this door already holding the estate's service credential. Measured on the sibling
    ingest door: 403 straight to the pod, 202 through the gateway.

    What that buys an anonymous caller here is not a read: `/produce` writes `bronze$events`,
    fabricates OpenLineage provenance, and fires the whole bronze->silver->gold cascade.
    """
    _expect(monkeypatch, 403, app_token="s3cr3t", dapr_token="s3cr3t", caller_app_id="gateway")


def test_the_TRAIN_door_inherits_the_refusal() -> None:
    """`authorize_train` delegates its whole decision to `authorize_produce`.

    An unforwarded caller id would leave `/train` — which spends GPU and writes the model registry —
    open while `/produce` looked fixed, and the delegation is precisely what makes that invisible.
    """
    ns = SimpleNamespace(oidc_enabled=True, produce_admin_project="acme", app_api_token="s3cr3t")
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=None))))

    with pytest.raises(LanceNamespaceError) as exc:
        asyncio.run(
            produce_auth.authorize_train(
                request,
                cast(MedallionSettings, ns),
                cast(OpenFgaClient, object()),
                dapr_api_token="s3cr3t",
                authorization=None,
                dapr_caller_app_id="gateway",
            )
        )
    assert status_for(int(exc.value.code)) == 403


def test_a_SERVICE_caller_and_a_DIRECT_caller_are_both_still_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not sever service-to-service produce, which is the token's actual job.

    Absent caller id = pub/sub, input-binding or Service-DNS delivery — every legitimate path onto
    this door. A fix that broke them would be indistinguishable from deleting the service-token path.
    """
    assert _run(monkeypatch, app_token="s3cr3t", dapr_token="s3cr3t", caller_app_id="medallion") is None
    assert _run(monkeypatch, app_token="s3cr3t", dapr_token="s3cr3t", caller_app_id=None) is None


def test_the_human_path_returns_the_verified_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The door is the LAST place the cascade's originator exists.

    It returned nothing, so a bronze->silver->gold run could never name the person who started it: by
    the time a later stage fails the request is gone and the mover authors as a chart role literal. The
    value is a TARGETING hint only — it rides `lance.originator` into the notifications plane, which
    re-derives every recipient's visibility at delivery — so returning it widens no authorization."""
    assert _run(monkeypatch, app_token="secret", authz="Bearer t", verifier=_Verifier("alice")) == "alice"


def test_the_service_path_names_no_originator(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared service token authenticates a SERVICE, not a person. `None` rather than a placeholder:
    an inbox addressed to a role is the defect this whole change exists to remove."""
    assert _run(monkeypatch, app_token="secret", dapr_token="secret") is None
