"""Root conftest — make the suite hermetic against the HARNESS's own environment.

There is exactly one rule here, and it exists because of a measured failure rather than a principle.

THE FAILURE. `dagger call test` would not finish: two runs were abandoned, one at 22 minutes and one
at 56, with the pytest worker sitting in `wchan=hrtimer_nanosleep` at ~1.7% CPU. It was not slow, it
was asleep. Dagger injects `OTEL_EXPORTER_OTLP_ENDPOINT` into every container it runs, for its OWN
telemetry — so any code that opts into OpenTelemetry by looking for that variable wired a live
exporter aimed at Dagger's collector, which rejects application metrics (`unknown aggregation from
pb`, in the engine's own log), and the SDK then retried with exponential backoff. Every app any test
built paid for it.

`service_kit.setup_otel` has since been fixed so an explicit `Settings` decides. But the fallback it
keeps is legitimate and load-bearing — `services/gateway` calls `setup_otel(app, service_name=...)`
with no `Settings` at all, at MODULE scope, and opts in through the endpoint alone. So merely
importing the gateway inside a telemetry-injecting harness still starts an exporter. The service is
right; the environment is what is wrong, and it is wrong for the whole session.

Hence: strip the ambient OTLP variables ONCE, for the session, before anything imports. This is the
same instinct as the config-isolation fixtures the per-directory conftests already use ("`create_app`
calls `load_dotenv`, so the suite pins env to stay hermetic") — one scope up, against a variable no
test sets and no test should inherit.

**"BEFORE ANYTHING IMPORTS" IS THE LOAD-BEARING WORD, AND THE FIRST VERSION DID NOT ACHIEVE IT.** The
strip lived only in the session-scoped autouse fixture below, and a session fixture runs on the FIRST
TEST — long after collection has imported every test module, and therefore long after any module-scope
`setup_otel` has already built its exporter. The variable was removed from an environment nothing was
going to read again. That is why the strip now also happens at this module's own import (see
`_strip_harness_otlp()` below the constant), which pytest performs before collection begins, and why
`tests/unit/test_conftest_otlp_strip.py` drives a real subprocess to prove the ordering rather than
asserting it from a comment.

WHAT THIS DELIBERATELY DOES NOT DO: it does not stop a test setting the variable itself.
`monkeypatch.setenv` still works and is still honoured — `test_setup_otel_wires_when_enabled` and
`test_NO_settings_still_opts_in_through_the_endpoint` both depend on that. Removing the ambient value
at session start and letting a test opt back in per-case is exactly the distinction between "the
harness leaked into the run" and "this test is exercising the enabled path".
"""

import os
import pathlib
from collections.abc import Iterator
from typing import Final

import pytest
import respx


#: The OTLP variables a CI harness may inject. `OTEL_EXPORTER_OTLP_ENDPOINT` is the one Dagger sets
#: and the one `setup_otel` keys its fallback on; the signal-specific overrides and the headers are
#: removed with it so a partially-stripped environment cannot produce a half-configured exporter,
#: which is harder to diagnose than either extreme.
_HARNESS_OTLP_VARS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
)


def _strip_harness_otlp() -> None:
    """Drop every ambient OTLP variable. Idempotent, and safe to call more than once."""
    for name in _HARNESS_OTLP_VARS:
        os.environ.pop(name, None)


# ── AT IMPORT, and this is the whole point ────────────────────────────────────────────────────────
# The first version of this file did the strip in the session fixture below and nowhere else, which
# does not work for the case it was written for. pytest's order is: load the rootdir conftest →
# COLLECT (which IMPORTS every test module) → run the first test (which is when a session-scoped
# autouse fixture finally executes). The damage this guards against is done at IMPORT: `services/gateway`
# calls `setup_otel(app, service_name=...)` at MODULE scope with no `Settings`, so the exporter is
# already wired by the time any fixture runs, and stripping the variable afterwards cannot un-wire it.
#
# Measured, not reasoned: with the strip in the fixture alone, a module importing the gateway under an
# injected `OTEL_EXPORTER_OTLP_ENDPOINT` still saw the variable set at module scope. Moved here, it
# sees `None`. A conftest at the rootdir is imported before collection begins, which is early enough.
_strip_harness_otlp()


# ── THE HARNESS IS A SERVICE RUN OUTSIDE THE CHART, AND IT SAYS SO ────────────────────────────────
# `assert_authentication_configured` refuses to boot a governed service whose authentication is off
# with nobody having acknowledged it (Q17-6 / §F2-2): "off because I meant it" and "off because
# nothing set it" are indistinguishable, and only the second is a vulnerability. A test harness is
# squarely the first — hundreds of integration tests build a real catalog app with `RASK_OIDC_ENABLED`
# unset ON PURPOSE, to exercise routing, identifier parsing and error mapping rather than auth.
#
# Declaring it here is what makes the control mean something. The alternative considered and rejected
# was exempting tests inside the assertion itself, which would leave the estate's own suites proving
# the behaviour of a path no deployment can take.
#
# AT IMPORT, for the same reason the OTLP strip is: apps are built at MODULE scope during collection,
# long before any fixture runs. `setdefault` rather than an assignment, so a test that wants the
# refusal — or wants auth genuinely ON — still decides for itself; the suite proving this guard
# depends on exactly that.
os.environ.setdefault("RASK_INSECURE_ALLOW_UNAUTHENTICATED", "true")

# THE SAME DECLARATION, for the same control one layer down. `require_dapr_token` refuses a door with
# no `APP_API_TOKEN` to compare against: a guard that cannot authenticate must not admit. Dozens of
# suites drive a Dapr-delivered route — the cron bindings, the pub/sub subscriptions, the DLQ drains —
# to exercise what the HANDLER does, and configure no token because the door is not what they are
# testing. That is "open because I meant it", and it is declared here rather than left implicit.
#
# It costs nothing in coverage, because the door has its own tests and they decide this variable for
# themselves: `test_annotation_jobs_gate.py` `delenv`s it and `test_produce_auth.py` sets it `false`,
# which `setdefault` leaves them free to do. The DEPLOYMENT half is gated separately by
# `test_every_dapr_door_has_a_token_to_check.py`, reading the render — so a service that ships with no
# token to check is caught there, not hidden here.
os.environ.setdefault("RASK_ALLOW_UNAUTHENTICATED_DAPR", "true")


@pytest.fixture(scope="session", autouse=True)
def _no_harness_telemetry() -> None:
    """Re-strip once for the session, AFTER every conftest and plugin has had its turn.

    Kept alongside the import-time call rather than replaced by it, because they close different
    holes. The import-time call is the one that matters and is the one that was missing. This one
    catches a re-introduction: the per-directory conftests build real apps, `create_app` calls
    `load_dotenv()`, and a `.env` on a developer box naming an OTLP endpoint would put the variable
    back after this module was imported.

    Not restored afterwards, on purpose. The values belong to the harness, nothing in the suite reads
    them once removed, and putting them back at teardown would only re-arm the exporter while pytest
    is still writing its report.
    """
    _strip_harness_otlp()


#: How long any test may let Dapr's sidecar handshake run. The SDK's own default is 60 s, and where
#: there is no sidecar NOTHING is cached, so every unstubbed `ActorProxy.create` pays it again — the
#: arithmetic `_no_dapr_proxy_factory_carryover` names below and the reason CI run 35726435185 spent
#: 8m17s in `wait_for_sidecar` before `--timeout=300` took the offline suite and four e2e lanes with it.
DAPR_HANDSHAKE_BUDGET_SECONDS: Final = 1.0


@pytest.fixture(autouse=True)
def _bounded_dapr_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the sidecar handshake for every test, WITHOUT stubbing the handshake itself.

    The distinction is the whole point, and it is the same one `_no_dapr_proxy_factory_carryover`
    makes below: a stub of `DaprHealth.wait_for_sidecar` would swap the handshake production runs for
    one it never runs, so a test that reaches the SDK would stop observing it. Lowering the BUDGET
    leaves the mechanism exactly as it is — it still polls, still sleeps, still raises — and only
    stops it costing a minute per call.

    Thirty of the estate's thirty-one Dapr-touching files mock at `typed_proxy`/`inbox_for` and never
    reach the SDK. This is for the thirty-first, whichever it turns out to be next: a per-file guard
    was added for the one that caused the last hang and the hang returned through a different file.

    A test that needs a specific duration sets its own inside a `MonkeyPatch.context` —
    function-scoped monkeypatch here loses to a narrower patch and is restored after.
    """
    try:
        from dapr.conf import settings as dapr_settings
    except ImportError:  # dapr is absent from a scoped sync (`dagger call test-package`)
        return
    monkeypatch.setattr(dapr_settings, "DAPR_HEALTH_TIMEOUT", DAPR_HANDSHAKE_BUDGET_SECONDS, raising=False)


@pytest.fixture(autouse=True)
def _no_dapr_proxy_factory_carryover() -> None:
    """Clear Dapr's PROCESS-GLOBAL actor-proxy factory before each test, so one test cannot decide
    another's outcome.

    Same family as the OTLP strip above — harness state leaking across a boundary the suite does not
    control — but the carrier is a class attribute rather than an env var. `ActorProxy` caches its
    default factory on the CLASS (`_default_proxy_factory`), and `_get_default_factory_instance`
    assigns it only after the constructor RETURNS. Two consequences, both observed:

    * A test that successfully builds one leaves it built for the rest of the process, so a later
      test that expects that construction to raise a `TimeoutError` (no sidecar) fails `DID NOT
      RAISE` whenever something warmed the cache first. Its result becomes a function of collection
      order, which is the definition of a test bug (F.I.R.S.T., Independent).
    * Where the constructor RAISES — no sidecar — nothing is cached, so every later call pays the full
      `DAPR_HEALTH_TIMEOUT` again. That is the 60 s-per-call arithmetic behind the CI hang.

    Deliberately NOT patching `DaprHealth.wait_for_sidecar` globally, for the reason
    `_dapr_handshake_budget` gives above. Resetting the cache leaves the mechanism intact and only
    removes the cross-test coupling.

    Cheap by construction: setting one class attribute to `None`. Tests that mock at a higher level
    (`typed_proxy`, `inbox_for` — thirty of the estate's thirty-one Dapr-touching files) never reach it.
    """
    try:
        from dapr.actor.client.proxy import ActorProxy
    except ImportError:  # dapr is not installed in every scoped sync (`dagger call test-package`)
        return
    ActorProxy._default_proxy_factory = None


# ── EVERY respx test in the estate ran in the one form that cannot see a dead route ────────────────
# respx's own default is strict: `MockRouter.__init__` takes assert_all_called=True, assert_all_mocked=True,
# and `writing-python/references/testing.md` states it as the rule — "every routed call must fire, and
# every unrouted call raises. That's usually what you want."
#
# That is true of the `respx_mock` FIXTURE and of the CALLED decorator form. It is not true of the bare
# one. `respx.mock` is a module-level MockRouter instance constructed with _assert_all_called = False,
# and the estate uses `@respx.mock` bare 118 times, the configured form 0 times, and sets the flag
# explicitly nowhere. So a route registered for a call the code no longer makes was silent everywhere,
# by construction.
#
# Measured when first switched on: 17 of 227 tests across the 16 respx files went red. The largest
# cluster is the register path, where thirteen tests mocked `POST /v1/namespace/{tier}/create` — a call
# the cascade is ruled never to make, and which answers 400 in-cluster, not the 200 the mock promised.
#
# Flipping the instance rather than editing 118 decorators is deliberate: the configured form
# `@respx.mock(assert_all_called=...)` builds a NEW router, so module-level `respx.post(...)` inside the
# test body would register on the global one and the call would raise "not mocked". One instance
# attribute covers every existing site and every future one.
#
# To assert a call is NOT made, do not register a phantom route for it. `assert_all_mocked` is already
# True, so an unregistered call raises AllMockedAssertionError on its own — and where the intent should
# be explicit, assert over the recorded calls instead:
#     assert not [c for c in respx.calls if "/namespace/" in str(c.request.url)]
respx.mock._assert_all_called = True


@pytest.fixture
def respx_allows_unused_routes() -> Iterator[None]:
    """Opt OUT of the estate default above, for the one shape where an uncalled route is the POINT.

    Two different things register a route that never fires, and until this fixture existed they were
    indistinguishable in the source:

    * a DEAD route — the code stopped making that call and nobody removed the mock. Thirteen of these
      sat in the register path, promising a 200 for a namespace-create the cascade is ruled never to
      make and the real catalog answers 400. That is what the default catches.
    * a NEGATIVE route — registered precisely so the test can prove it was NOT called, or prove which
      SUBSET fired. `test_max_pages_caps_the_FETCHES_not_only_the_result` is the clearest: it registers
      five image routes and asserts `[True, True, False, False, False]`, so the cap is proven by which
      routes fired. Deleting the last three would delete the proof.

    Taking this fixture is the declaration that a test is the second kind. It is deliberately a
    fixture rather than a decorator argument: `@respx.mock(assert_all_called=False)` builds a NEW
    router, so module-level `respx.post(...)` in the test body would register on the global one and the
    call would raise "not mocked" — the opt-out would silently change what the test exercises.
    """
    respx.mock._assert_all_called = False
    try:
        yield
    finally:
        respx.mock._assert_all_called = True


@pytest.fixture(autouse=True)
def _reset_pooled_ray_client() -> Iterator[None]:
    """Drop `ray_submit`'s module-level Ray client between tests.

    That client is pooled for the WORKER's lifetime — a Dapr workflow activity has no `Request` and no
    reachable `app.state`, so that is the only lifetime available to it (open_fastapi-audit). Under
    pytest the "worker" is the whole session, so without this a client built by one test is reused by
    the next, and a later test installing a `MockTransport` through `httpx.AsyncClient` never sees it
    because no new client is constructed.

    In the ROOT conftest rather than per-suite: the affected tests live in both `services/medallion/
    tests/` and `tests/unit/`, and two copies of a reset fixture is the drift this file exists to
    avoid. Import is local and guarded so a run that never touches medallion pays nothing.
    """
    yield
    try:
        from medallion.services.ray_submit import close_ray_client
    except ImportError:  # pragma: no cover - medallion not installed in this env
        return
    import asyncio

    asyncio.run(close_ray_client())


class ServiceAccountIssuer:
    """A Kubernetes service-account issuer on loopback, for the doors that verify projected SA tokens ([[LH-220]]).

    It answers as the k3s API server does, measured 2026-10-02 (the LH-220 P5.3 probes): discovery and the key set
    are served only over TLS from a private CA and only to a bearer (`--anonymous-auth=false` answers 401 to an
    anonymous fetch), and a token names `system:serviceaccount:<namespace>:<sa>` as its `sub`. So a verifier that
    reaches it has carried both the fetch credential and the CA, as it must in the cluster.
    """

    def __init__(self, directory: "pathlib.Path") -> None:
        import datetime
        import http.server
        import ipaddress
        import json
        import secrets
        import ssl
        import threading

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        self._signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-sa-key"
        numbers = self._signing_key.public_key().public_numbers()

        def b64(value: int) -> str:
            import base64

            return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

        jwks = {"keys": [{"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256", "n": b64(numbers.n), "e": b64(numbers.e)}]}
        self.fetch_token = secrets.token_urlsafe(24)
        self.fetch_token_file = directory / "fetch-token"
        self.fetch_token_file.write_text(self.fetch_token)

        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.UTC)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-sa-issuer-ca")])
        ca = (
            x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .sign(ca_key, hashes.SHA256())
        )  # fmt: skip
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        server_cert = (
            x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])).issuer_name(ca_name)
            .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )  # fmt: skip
        self.ca_file = directory / "ca.crt"
        self.ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        chain = directory / "server.pem"
        chain.write_bytes(
            server_cert.public_bytes(serialization.Encoding.PEM)
            + server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        )

        issuer_ref: dict[str, str] = {}
        demanded = f"Bearer {self.fetch_token}"
        self.fetches: list[str] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self_outer.fetches.append(self.path)
                if self.headers.get("Authorization") != demanded:
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.path == "/.well-known/openid-configuration":
                    body = {
                        "issuer": issuer_ref["issuer"],
                        "jwks_uri": f"{issuer_ref['issuer']}/openid/v1/jwks",
                        "id_token_signing_alg_values_supported": ["RS256"],
                    }
                elif self.path == "/openid/v1/jwks":
                    body = jwks
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self_outer = self
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(chain)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.issuer = issuer_ref["issuer"] = f"https://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def mint(self, sa: str, *, audience: str, namespace: str = "default", ttl: int = 600, issued_at: int | None = None) -> str:
        """A projected token for `namespace/sa`, shaped as the kubelet's are (claims measured on k3s 2026-10-02)."""
        import time
        import uuid

        import jwt

        iat = int(time.time()) if issued_at is None else issued_at
        claims = {
            "iss": self.issuer,
            "sub": f"system:serviceaccount:{namespace}:{sa}",
            "aud": [audience],
            "iat": iat,
            "nbf": iat,
            "exp": iat + ttl,
            "jti": str(uuid.uuid4()),
            "kubernetes.io": {"namespace": namespace, "serviceaccount": {"name": sa, "uid": str(uuid.uuid4())}},
        }
        return jwt.encode(claims, self._signing_key, algorithm="RS256", headers={"kid": self.kid})

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(scope="session")
def sa_issuer(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServiceAccountIssuer]:
    """One loopback service-account issuer per worker; read-only, so session scope holds."""
    issuer = ServiceAccountIssuer(tmp_path_factory.mktemp("sa-issuer"))
    yield issuer
    issuer.close()
