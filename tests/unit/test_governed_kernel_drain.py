"""The `service_kit.governed` kernel's own contracts, pinned where the audit found them broken.

Each test names the finding it closes and fails for that finding's own reason without its fix:

* ``SKG-02`` — a per-tuple delete loop that aborts mid-way must still audit the tuples that WERE
  removed. Without it the trail says a grant is still there while OpenFGA says it is gone.
* ``SKG-03`` — ``expand_tree``'s cycle detection is PATH-scoped. A walk-global visited set labels an
  ordinary diamond (two branches meeting the same rung) a loop and drops the whole subtree, so the
  derivation a reader came for is silently missing.
* ``SKG-04`` — a truncated ``read_object_tuples`` must not return a short list that reads as complete;
  a revoke built on it would leave live grants behind while reporting success.
* ``SKG-05`` — the 401 leaves on the SAME taxonomy as this door's other refusals, so a missing bearer
  and an invalid one are described the same way; the 503 deliberately does not (see the test).
* ``SKG-12`` — the JWKS fetch carries an explicit timeout, like the discovery fetch beside it.
* ``SKG-15`` — a secret-store fetch failure is an ERROR, and a non-transport fault is NOT swallowed
  into an empty bundle that reads as "the store holds nothing".
* ``SKG-10`` — the environment this kernel reads is DECLARED (a settings class, the injected sidecar
  port), not four bare ``os.environ`` calls three of which name the same variable.
* ``SKG-16`` — ``_retrying`` is annotated, and ``AuthDeps`` names callables rather than ``Any``.

Uses stdlib ``asyncio.run`` so no pytest-asyncio dependency is needed (matches its sibling suites).
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, cast

import aiohttp
import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientTuple
from pydantic import BaseModel

from service_kit.exceptions import register_handlers
from service_kit.governed import dapr_auth, fga, oidc, secrets
from service_kit.governed.audit import AUDIT_LOGGER, configure_audit
from service_kit.governed.deps import make_auth_deps
from service_kit.lakehouse.ns_errors import install_problem_handlers


def _client(c: object) -> OpenFgaClient:
    """Cast a fake to the SDK client type (it only needs the called methods)."""
    return cast(OpenFgaClient, c)


class _AuditCapture(logging.Handler):
    """Collect the structured audit records emitted while a block runs."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def audit_rows() -> Any:
    """The audit stream, enabled and captured for the duration of one test."""
    configure_audit(enabled=True)
    handler = _AuditCapture()
    log = logging.getLogger(AUDIT_LOGGER)
    log.addHandler(handler)
    try:
        yield handler.records
    finally:
        log.removeHandler(handler)
        configure_audit(enabled=False)


# --------------------------------------------------------------------------- #
# SKG-02 — a partial delete still audits what landed
# --------------------------------------------------------------------------- #


def test_delete_tuples_audits_the_tuples_that_landed_before_it_fails(audit_rows: list[logging.LogRecord]) -> None:
    """SKG-02: `delete_tuples` deletes one tuple per write and raises from INSIDE that loop.

    The tuple that WAS removed is gone from OpenFGA whatever happens next, so the trail owes an
    `access_tuple_delete` row for it. Auditing only after the loop returns means an outage on tuple
    two erases the record of tuple one — and the estate then believes a revoked grant is still live.
    """

    class _FlakyDeleteClient:
        def __init__(self) -> None:
            self.writes = 0

        async def write(self, body: Any, *_a: object, **_k: object) -> None:
            del body
            self.writes += 1
            if self.writes > 1:
                raise aiohttp.ClientConnectionError("connection refused")

    doomed = [
        ClientTuple(user="user:alice", relation="owner", object="table:t"),
        ClientTuple(user="user:bob", relation="reader", object="table:t"),
    ]
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.delete_tuples(
                _client(_FlakyDeleteClient()),
                doomed,
                actor="admin",
                origin="admin_api",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )

    removed = [r for r in audit_rows if getattr(r, "audit.action", None) == "access_tuple_delete"]
    assert [getattr(r, "audit.grantee") for r in removed] == ["user:alice"], (
        "the tuple OpenFGA confirmed removed must be audited even though a later tuple failed"
    )


# --------------------------------------------------------------------------- #
# SKG-03 — a diamond is not a cycle
# --------------------------------------------------------------------------- #


def _leaf_node(name: str, *, computed: str | None = None, users: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        leaf=SimpleNamespace(
            computed=SimpleNamespace(userset=computed) if computed else None,
            tuple_to_userset=None,
            users=SimpleNamespace(users=users) if users is not None else None,
        ),
    )


class _DiamondClient:
    """`can_read` reaches `owner` through BOTH `writer` and `reader` — a diamond, not a loop."""

    _TREE = {
        "can_read": None,  # union, built below
        "writer": _leaf_node("table:t#writer", computed="table:t#owner"),
        "reader": _leaf_node("table:t#reader", computed="table:t#owner"),
        "owner": _leaf_node("table:t#owner", users=["user:alice"]),
    }

    async def expand(self, body: Any, *_a: object, **_k: object) -> object:
        relation = body.relation
        if relation == "can_read":
            root = SimpleNamespace(
                name="table:t#can_read",
                union=SimpleNamespace(
                    nodes=[
                        _leaf_node("table:t#can_read", computed="table:t#writer"),
                        _leaf_node("table:t#can_read", computed="table:t#reader"),
                    ]
                ),
            )
        else:
            root = self._TREE[relation]
        return SimpleNamespace(tree=SimpleNamespace(root=root))


def test_expand_tree_does_not_call_a_diamond_a_cycle() -> None:
    """SKG-03: two branches meeting the same `object#relation` is CONCENTRIC, not cyclic.

    A walk-global visited set marks the second arrival `cycle: True` and returns a stub, so the
    `reader` branch of this diamond loses the `owner` rung and the subject holding it — the exact
    derivation `expand_tree` exists to show. Path-scoped tracking keeps both branches whole and still
    catches a genuine loop (pinned below).
    """
    tree = asyncio.run(
        fga.expand_tree(
            _client(_DiamondClient()),
            relation="can_read",
            obj="table:t",
            max_depth=4,
            retry_attempts=1,
            retry_backoff_seconds=0.0,
            retry_max_backoff_seconds=0.0,
        )
    )
    branches = tree["union"]
    assert len(branches) == 2
    holders = []
    for branch in branches:
        rung = branch["leaf"]["expanded"][0]
        assert not rung.get("cycle"), f"a diamond arm was mislabelled a cycle: {rung}"
        owner = rung["leaf"]["expanded"][0]
        assert not owner.get("cycle"), f"the shared rung was mislabelled a cycle: {owner}"
        holders.append(owner["leaf"]["users"])
    assert holders == [["user:alice"], ["user:alice"]]


def test_expand_tree_still_reports_a_real_cycle() -> None:
    """A relation that expands to itself must terminate as `cycle`, never recurse forever."""

    class _LoopClient:
        async def expand(self, body: Any, *_a: object, **_k: object) -> object:
            del body
            return SimpleNamespace(tree=SimpleNamespace(root=_leaf_node("namespace:n#owner", computed="namespace:n#owner")))

    tree = asyncio.run(
        fga.expand_tree(
            _client(_LoopClient()),
            relation="owner",
            obj="namespace:n",
            max_depth=5,
            retry_attempts=1,
            retry_backoff_seconds=0.0,
            retry_max_backoff_seconds=0.0,
        )
    )
    assert tree["leaf"]["expanded"][0] == {"name": "namespace:n#owner", "cycle": True}


# --------------------------------------------------------------------------- #
# SKG-04 — a truncated read is not a complete one
# --------------------------------------------------------------------------- #


def test_read_object_tuples_refuses_to_return_a_truncated_set() -> None:
    """SKG-04: hitting the page ceiling with a token still outstanding is a PARTIAL answer.

    `revoke_object_tuples` deletes exactly what this returns, so a short list is a revoke that leaves
    live grants behind and reports success. A warning tells an operator later; the caller must be
    told now, and the module's fail-closed posture is the way it tells them.
    """

    class _EndlessClient:
        def __init__(self) -> None:
            self.reads = 0

        async def read(self, _body: Any = None, options: Any = None, *_a: object, **_k: object) -> object:
            del options
            self.reads += 1
            return SimpleNamespace(
                tuples=[SimpleNamespace(key=SimpleNamespace(user=f"user:u{self.reads}", relation="reader", object="table:t", condition=None))],
                continuation_token="more",
            )

    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.read_object_tuples(
                _client(_EndlessClient()),
                "table:t",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


# --------------------------------------------------------------------------- #
# SKG-05 — the auth dependencies speak the taxonomy both planes map
# --------------------------------------------------------------------------- #


class _AuthSettings(BaseModel):
    oidc_enabled: bool = True
    fga_enabled: bool = False


def _auth_settings() -> _AuthSettings:
    return _AuthSettings()


# MODULE level, not inside the app factory: this file carries `from __future__ import annotations`, so
# a `Depends(_deps.…)` referencing a LOCAL name resolves to nothing and FastAPI silently demotes the
# parameter to a query param (a 422, never the refusal under test). `test_auth_deps_resolve.py` records
# the same trap — it is the reason endpoint modules bind their deps at module scope.
_AuthSettingsDep = Annotated[_AuthSettings, Depends(_auth_settings)]
_auth_deps = make_auth_deps(_AuthSettingsDep)
_Subject = Annotated[str, Depends(_auth_deps.current_subject)]
_OptionalSubject = Annotated[str, Depends(_auth_deps.optional_subject)]


def _governed_app() -> FastAPI:
    """An app wired the way every consumer of `make_auth_deps` actually wires one: BOTH installers.

    Verified rather than assumed — `annotator/main.py:186,196` calls `register_handlers` then
    `install_problem_handlers`, and `make_service_app` (notifications, controlplane) installs the same
    pair. Building this with only one of them would prove a defect in a configuration nothing ships.
    """
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))

    @app.get("/gated")
    def _gated(subject: _Subject) -> dict[str, str]:
        return {"subject": subject}

    @app.get("/soft")
    def _soft(subject: _OptionalSubject) -> dict[str, str]:
        return {"subject": subject}

    return app


def test_a_missing_bearer_is_refused_in_the_spec_taxonomy() -> None:
    """SKG-05: one dependency answered two adjacent cases in two envelopes.

    A BAD bearer already left `authenticate` as a `lance_namespace.UnauthenticatedError` (raised inside
    `verify`), which renders with the numeric `code` a Lance-Namespace client parses. A MISSING bearer
    left as the fleet `UnauthorizedError`, whose problem body carries no `code` at all — so the same
    door described "you are not authenticated" two different ways depending on which half failed.
    """
    with TestClient(_governed_app(), raise_server_exceptions=False) as client:
        client.app.state.oidc = object()
        refusal = client.get("/gated")
        assert refusal.status_code == 401
        assert refusal.headers["content-type"].startswith("application/problem+json")
        body = refusal.json()
        assert body["code"] == int(UnauthenticatedError("x").code)
        assert "bearer" in body["detail"].lower()


def test_the_unwired_verifier_503_still_names_the_knob() -> None:
    """The DELIBERATE other half of the split — see `deps.py`'s import comment.

    `ns_errors.problem_detail` redacts every 5xx detail but 501, so moving this refusal to the lance
    taxonomy would answer "Internal Server Error" and delete the only string that says which knob is
    unwired. This pins the message so the split cannot be quietly completed in the wrong direction.
    """
    with TestClient(_governed_app(), raise_server_exceptions=False) as client:
        unavailable = client.get("/gated")
        assert unavailable.status_code == 503
        assert unavailable.headers["content-type"].startswith("application/problem+json")
        assert "unavailable" in unavailable.json()["detail"].lower()

        soft = client.get("/soft")
        assert soft.status_code == 503
        assert "unavailable" in soft.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# SKG-12 — the JWKS fetch is bounded
# --------------------------------------------------------------------------- #


def test_jwks_client_is_built_with_an_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKG-12: an unbounded JWKS fetch parks a request worker on a hung IdP.

    The discovery fetch three lines above it is bounded; the key fetch was not, and it is the one on
    the per-token path (the discovery document is cached, a rotated `kid` is not).
    """
    captured: dict[str, Any] = {}

    class _FakeJWKClient:
        def __init__(self, uri: str, **kwargs: Any) -> None:
            captured["uri"] = uri
            captured.update(kwargs)

    def _fake_get(self: Any, url: str, *a: object, **k: object) -> httpx.Response:
        del self, a, k
        return httpx.Response(
            200,
            json={
                "issuer": "https://idp.example",
                "jwks_uri": "https://idp.example/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(oidc.jwt, "PyJWKClient", _FakeJWKClient)
    monkeypatch.setattr(httpx.Client, "get", _fake_get)

    verifier = oidc.OIDCVerifier(issuer="https://idp.example", audience="rask", cache_ttl=60)
    verifier._resolve("https://idp.example")

    assert captured["timeout"] == oidc.HTTP_FETCH_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# SKG-15 — a boot-blocking secret failure is loud, and never swallowed
# --------------------------------------------------------------------------- #


def test_secret_fetch_does_not_swallow_a_non_transport_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKG-15: `except Exception -> {}` turns a bug into "the store holds nothing".

    An empty bundle is indistinguishable from a store that legitimately holds no keys, and the fetch
    is on the boot path, so a defect here becomes a service that comes up unconfigured. Only the
    failures the retry loop is ABOUT (transport / HTTP status) may be reported as an empty bundle.
    """

    def _boom(*_a: object, **_k: object) -> httpx.Response:
        raise RuntimeError("a defect in the fetch path, not an unreachable store")

    monkeypatch.setattr(secrets.httpx, "get", _boom)
    with pytest.raises(RuntimeError):
        secrets.fetch_dapr_secret("store", "key", retries=1, backoff=0.0)


def test_exhausted_secret_fetch_logs_at_error(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A boot-blocking failure is an ERROR: at WARNING it sits under the default alert threshold."""

    def _unreachable(*_a: object, **_k: object) -> httpx.Response:
        raise httpx.ConnectError("no sidecar")

    monkeypatch.setattr(secrets.httpx, "get", _unreachable)
    with caplog.at_level(logging.DEBUG, logger=secrets.__name__):
        assert secrets.fetch_dapr_secret("store", "key", retries=1, backoff=0.0) == {}
    failures = [r for r in caplog.records if r.message == "dapr_secret_fetch_failed"]
    assert failures and failures[0].levelno == logging.ERROR


# --------------------------------------------------------------------------- #
# SKG-16 — the public seams are typed
# --------------------------------------------------------------------------- #


def test_governed_public_signatures_carry_real_types() -> None:
    """SKG-16: `Any` where a Callable/Protocol exists, and a decorator factory with no return type.

    Read off `__annotations__` rather than the source so the check cannot pass on a comment.
    """
    from service_kit.governed.deps import AuthDeps

    # `deps` deliberately carries no `from __future__ import annotations` (see its module comment), so
    # these are the evaluated objects, not strings.
    init = AuthDeps.__init__.__annotations__
    assert set(init) == {"authenticate", "current_subject", "get_checker", "get_fga_client", "optional_subject", "return"}
    assert [name for name, ann in init.items() if ann is Any] == [], f"AuthDeps still takes Any: {init}"
    assert "return" in fga._retrying.__annotations__, "_retrying has no return annotation"


def test_unauthenticated_error_is_the_lance_one() -> None:
    """The kernel raises ONE taxonomy; this pins the import the SKG-05 fix depends on."""
    assert issubclass(UnauthenticatedError, Exception)


# --------------------------------------------------------------------------- #
# SKG-10 — the environment this kernel reads is declared, not grepped for
# --------------------------------------------------------------------------- #


def test_the_dapr_door_reads_its_environment_through_a_settings_class() -> None:
    """SKG-10: four bare `os.environ.get` calls, three of them naming the same variable.

    Asserted on the module SOURCE because that is exactly what the finding is about — a variable this
    door authenticates against must be declared once, not discoverable only by grep.
    """
    tree = ast.parse(Path(dapr_auth.__file__).read_text())
    env_reads = [
        f"line {node.lineno}: os.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"getenv", "environ", "get"}
        and ast.unparse(node.func).startswith("os.")
    ]
    assert env_reads == [], f"dapr_auth still reads the environment outside its settings class: {env_reads}"
    declared = {name: field.alias for name, field in dapr_auth.DaprDoorSettings.model_fields.items()}
    assert declared == {"app_api_token": "APP_API_TOKEN", "public_callers": "RASK_PUBLIC_CALLERS"}


def test_the_secret_fetch_uses_the_injected_sidecar_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKG-10: 3500 was hard-coded, so a pod whose sidecar is elsewhere fetched from a closed port.

    daprd injects `DAPR_HTTP_PORT`; `governed/actor_state_store.py` has always read it, and this puts
    the secret fetch on the same source instead of on a literal.
    """
    seen: list[str] = []

    def _capture(url: str, *_a: object, **_k: object) -> httpx.Response:
        seen.append(url)
        return httpx.Response(200, json={"service-token-x": "v"}, request=httpx.Request("GET", url))

    monkeypatch.setenv("DAPR_HTTP_PORT", "3999")
    monkeypatch.setattr(secrets.httpx, "get", _capture)
    assert secrets.fetch_dapr_secret("store", "key", retries=1, backoff=0.0) == {"service-token-x": "v"}
    assert seen == ["http://localhost:3999/v1.0/secrets/store/key"]
