"""Fail-closed dual-auth for the /produce + /train triggers (#64): a service account or a project-admin OIDC person.

The cascade head is provenance-fabricatable, so the admin door added for the UI must not be bypassable:
an invalid bearer 401s, a non-admin 403s, an FGA outage 503s (never a silent allow), and a request carrying
no credential 403s. A door with nothing to authenticate a caller refuses unless the deployment sets
`RASK_INSECURE_ALLOW_UNAUTHENTICATED`. A SERVICE is admitted by its projected service-account token and
authorized on FGA exactly like a person ([[LH-220]]); its behaviour against a real verifier is pinned in
`services/medallion/tests/test_the_operator_doors_authorize_on_the_resource.py`.

Two layers: direct-function tests pin every fail-closed branch of :func:`authorize_produce` (sync via
``asyncio.run`` — no async-plugin dependency); the TestClient tests pin that it is actually WIRED onto the
``/produce`` route (a direct, non-sidecar POST is gated end-to-end), which the function tests can't prove.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import TypedDict, Unpack, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespaceError, PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient

from medallion.api import produce_auth
from medallion.api.dependencies import get_dapr, get_settings
from medallion.api.produce import router
from medallion.api.train import router as train_router
from medallion.core.config import MedallionSettings
from service_kit.governed.audit import AUDIT_LOGGER, configure_audit
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.lakehouse.ns_errors import install_problem_handlers, status_for


# ── direct-function tests: every fail-closed branch of authorize_produce ──────────────────────────


class _Verifier:
    def __init__(self, sub: str = "alice", *, invalid: bool = False) -> None:
        self._sub, self._invalid = sub, invalid

    def verify(self, _token: str) -> object:  # the fake ignores the token
        if self._invalid:
            raise UnauthenticatedError("bad token")
        return SimpleNamespace(sub=self._sub)


class _ServiceVerifier:
    """The service-account verifier's two calls the door makes: whose token this is, and who it proves."""

    def issued(self, token: str) -> bool:
        return token.startswith("sa.")

    def verify(self, _token: str) -> ServicePrincipal:
        return ServicePrincipal(subject="service-ingest", service_account="system:serviceaccount:rask:rask-sa-ingest")


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    authz: str | None = None,
    verifier: object | None = None,
    service_verifier: object | None = None,
    oidc_enabled: bool = True,
    fga_result: bool = True,
    fga_raises: bool = False,
    project: str | None = None,
    caller_app_id: str | None = None,
    captured: dict[str, object] | None = None,
    wired: bool = True,
) -> str | None:
    async def fake_check(_client: object, **kw: object) -> bool:  # user=/relation=/obj= arrive as kwargs
        if captured is not None:
            captured.update(kw)
        if fga_raises:
            raise ServiceUnavailableError("fga down")
        return fga_result

    monkeypatch.setattr(produce_auth.fga, "check", fake_check)
    ns = SimpleNamespace(oidc_enabled=oidc_enabled, produce_admin_project="acme", sa_issuer=None, insecure_allow_unauthenticated=False)
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=verifier, sa_oidc=service_verifier))))
    settings = cast(MedallionSettings, ns)
    return asyncio.run(
        produce_auth.authorize_produce(
            request,
            settings,
            cast(OpenFgaClient, object()) if wired else None,
            authorization=authz,
            project=project,
            dapr_caller_app_id=caller_app_id,
        )
    )


class _RunArgs(TypedDict, total=False):
    """`_run`'s keywords and their types, so a value passed through `_expect` is checked against them."""

    authz: str | None
    verifier: object | None
    service_verifier: object | None
    oidc_enabled: bool
    fga_result: bool
    fga_raises: bool
    project: str | None
    caller_app_id: str | None
    captured: dict[str, object] | None
    wired: bool


def _expect(monkeypatch: pytest.MonkeyPatch, status: int, **kw: Unpack[_RunArgs]) -> None:
    # The gate raises the lance_namespace domain errors (same taxonomy as catalog/lineage security), so the
    # HTTP status is the ns_errors mapping of the error code, not an HTTPException attribute.
    with pytest.raises(LanceNamespaceError) as exc:
        _run(monkeypatch, **kw)
    assert status_for(int(exc.value.code)) == status


# ── an UNCONFIGURED door admits nobody unless the deployment says it runs open ────────────────────
# A door with no OIDC and no service-account issuer cannot authenticate anybody, so it admits nobody.
# `RASK_INSECURE_ALLOW_UNAUTHENTICATED` is how a deployment that means to run open says so, explicitly and
# greppably, where an empty setting is neither.


class _Settings:
    """A door with neither verifier configured, and whether its operator acknowledged that."""

    oidc_enabled = False
    sa_issuer = None
    produce_admin_project = "acme"

    def __init__(self, *, open_door: bool) -> None:
        self.insecure_allow_unauthenticated = open_door


async def _call(*, open_door: bool) -> str | None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=None, sa_oidc=None)))
    return await produce_auth.authorize_produce(cast(Request, request), cast(MedallionSettings, _Settings(open_door=open_door)), cast(OpenFgaClient, object()))


@pytest.mark.asyncio
async def test_an_unconfigured_door_refuses() -> None:
    with pytest.raises(PermissionDeniedError):
        await _call(open_door=False)


@pytest.mark.asyncio
async def test_the_hatch_still_opens_it() -> None:
    """The control. Without it, a door that refused unconditionally would pass the case above.

    A deployment that means to run open says so, and gets exactly what it had before: admitted, with no
    verified subject to carry as an originator.
    """
    assert await _call(open_door=True) is None


def test_invalid_bearer_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 401, authz="Bearer bad", verifier=_Verifier(invalid=True))


def test_malformed_authorization_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _expect(monkeypatch, 401, authz="Basic xyz", verifier=_Verifier())


# ── the bearer is verified OFF the event loop (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" ING-02, on this door) ─────────────


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
    assert _run(monkeypatch, authz="Bearer good", verifier=verifier) == "alice"
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


def test_an_UNWIRED_fga_client_is_503_even_for_an_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check would allow; only the missing client can refuse, and it must, never as an allow."""
    _expect(monkeypatch, 503, authz="Bearer good", verifier=_Verifier(), fga_result=True, wired=False)


def test_bearer_but_oidc_disabled_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bearer is presented but OIDC is off → the human door is shut; never a silent allow.
    _expect(monkeypatch, 403, authz="Bearer good", verifier=_Verifier(), oidc_enabled=False)


def test_bearer_but_unwired_verifier_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    # OIDC enabled but app.state.oidc was never wired (startup/discovery skew): a bearer-presenting caller
    # must surface the auth-layer OUTAGE (503, the catalog/lineage security.py invariant), never the
    # terminal 403 — a valid admin would otherwise be misreported as denied, and 503-keyed monitoring
    # (which the FGA-unwired branch already feeds) would miss the misconfiguration.
    _expect(monkeypatch, 503, authz="Bearer good", verifier=None)


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
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        oidc_enabled=False, produce_admin_project="acme", sa_issuer=None, insecure_allow_unauthenticated=False
    )
    return TestClient(app, raise_server_exceptions=False)


def test_route_rejects_a_request_with_no_service_account_and_no_person() -> None:
    # The `dapr-api-token` a sidecar stamps names nobody, so it is no credential at this door.
    assert _client().post("/produce", headers={"dapr-api-token": "nope"}).status_code == 403


# ── #84 per-tenant produce: the admin gate follows the REQUESTED project ───────────────────────────


def test_oidc_admin_gate_targets_the_requested_project(monkeypatch: pytest.MonkeyPatch) -> None:
    # A caller producing into project X must administer X — not the fixed configured project.
    captured: dict[str, object] = {}
    _run(
        monkeypatch,
        authz="Bearer good",
        verifier=_Verifier(),
        project="globex",
        captured=captured,
    )
    assert captured["obj"] == "project:globex"


def test_route_rejects_a_malformed_project_with_422() -> None:
    # The project becomes an S3 prefix + lineage qualifier — a path-shaped value is refused at the edge.
    res = _client().post("/produce", params={"project": "../evil"}, headers={"Idempotency-Key": "idem-test"})
    assert res.status_code == 422
    assert {e["field"] for e in res.json()["errors"]} == {"query.project"}, res.text


def test_produce_route_409s_when_project_routing_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dev-open door + real settings (control_root unset): a project-carrying produce is REFUSED (409,
    # problem+json), never silently seeded into the shared root.
    monkeypatch.setenv("RASK_INSECURE_ALLOW_UNAUTHENTICATED", "true")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_dapr] = lambda: None
    app.dependency_overrides[get_settings] = lambda: MedallionSettings.model_validate({})
    res = TestClient(app, raise_server_exceptions=False).post("/produce", params={"project": "acme"}, headers={"Idempotency-Key": "idem-test"})
    assert res.status_code == 409
    assert res.headers["content-type"].startswith("application/problem+json")


# ── /train gate: pinned to the CONFIGURED project — a caller-supplied ?project= is ignored ─────────


def test_train_gate_checks_the_configured_project(monkeypatch: pytest.MonkeyPatch) -> None:
    # The OIDC admin door through authorize_train always targets the CONFIGURED project.
    captured: dict[str, object] = {}

    async def fake_check(_client: object, **kw: object) -> bool:
        captured.update(kw)
        return True

    monkeypatch.setattr(produce_auth.fga, "check", fake_check)
    ns = SimpleNamespace(oidc_enabled=True, produce_admin_project="acme", sa_issuer=None, insecure_allow_unauthenticated=False)
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=_Verifier(), sa_oidc=None))))
    asyncio.run(
        produce_auth.authorize_train(
            request,
            cast(MedallionSettings, ns),
            cast(OpenFgaClient, object()),
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
    # signal distinct from the guard's own 403); the acknowledged-open door admits the request with no subject.
    app.dependency_overrides[get_settings] = lambda: SimpleNamespace(
        oidc_enabled=False, produce_admin_project="acme", sa_issuer=None, insecure_allow_unauthenticated=True, ray_enabled=False, s3_endpoint="", bronze_uri=""
    )
    return TestClient(app, raise_server_exceptions=False)


_TRAIN_BODY = {"model": "m1", "features": [{"dataset": "silver$feats"}]}


def test_train_route_ignores_a_caller_supplied_project() -> None:
    # ?project=other on /train: the stray param is IGNORED — the guard passes (pinned to the configured
    # project) and the request proceeds to the handler (here the disabled-head 409).
    res = _train_client().post("/train", params={"project": "globex"}, json=_TRAIN_BODY, headers={"Idempotency-Key": "idem-test"})
    assert res.status_code == 409, res.text


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
    _run(monkeypatch, authz="Bearer good", verifier=_Verifier(), fga_result=True)
    assert len(audit_records) == 1
    assert _audit_fields(audit_records[0]) == {
        "audit.action": "can_administer",
        "audit.outcome": "allow",
        "audit.subject": "alice",
        "audit.resource": "project:acme",
    }


def test_admin_deny_is_audited(monkeypatch: pytest.MonkeyPatch, audit_records: list[logging.LogRecord]) -> None:
    _expect(monkeypatch, 403, authz="Bearer good", verifier=_Verifier(), fga_result=False)
    fields = _audit_fields(audit_records[0])
    assert fields["audit.action"] == "can_administer" and fields["audit.outcome"] == "deny"


def test_fga_outage_is_audited_as_failure(monkeypatch: pytest.MonkeyPatch, audit_records: list[logging.LogRecord]) -> None:
    _expect(monkeypatch, 503, authz="Bearer good", verifier=_Verifier(), fga_raises=True)
    fields = _audit_fields(audit_records[0])
    assert fields["audit.outcome"] == "failure" and fields["audit.reason"] == "authz_unavailable"


# ── the gateway must not launder anonymous traffic into a governed write ──────


def test_a_PUBLIC_callers_refusal_is_audited_on_the_REQUESTED_project(monkeypatch: pytest.MonkeyPatch, audit_records: list[logging.LogRecord]) -> None:
    """A service never arrives through a public front door, so one that does is refused before anything is
    authorized. `?project=` is this door's write target, so the refusal names it rather than the configured one."""
    _expect(monkeypatch, 403, authz="Bearer sa.token", service_verifier=_ServiceVerifier(), caller_app_id="gateway", project="beta")

    assert [_audit_fields(record) for record in audit_records] == [
        {"audit.action": "authn", "audit.outcome": "deny", "audit.subject": "service-ingest", "audit.resource": "project:beta", "audit.reason": "public_caller"}
    ]


def test_the_TRAIN_door_inherits_the_refusal() -> None:
    """`authorize_train` delegates its whole decision to `authorize_produce`.

    An unforwarded caller id would leave `/train` — which spends GPU and writes the model registry —
    open while `/produce` looked fixed, and the delegation is precisely what makes that invisible.
    """
    ns = SimpleNamespace(oidc_enabled=True, produce_admin_project="acme", sa_issuer=None, insecure_allow_unauthenticated=False)
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(oidc=None, sa_oidc=_ServiceVerifier()))))

    with pytest.raises(LanceNamespaceError) as exc:
        asyncio.run(
            produce_auth.authorize_train(
                request,
                cast(MedallionSettings, ns),
                cast(OpenFgaClient, object()),
                authorization="Bearer sa.token",
                dapr_caller_app_id="gateway",
            )
        )
    assert status_for(int(exc.value.code)) == 403


def test_a_service_is_authorized_on_FGA_like_a_person_and_is_never_an_originator(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cluster vouched for the account, and its subject is then checked on the project exactly as a
    person's is. It names no inbox, so the originator the route hands the cascade is `None`."""
    captured: dict[str, object] = {}

    originator = _run(monkeypatch, authz="Bearer sa.token", service_verifier=_ServiceVerifier(), captured=captured)

    assert originator is None
    assert (captured["user"], captured["obj"]) == ("service-ingest", "project:acme")
