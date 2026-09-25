"""A bearer the verifier cannot use is answered 401 or 503 at the governed doors, and never 500.

`OIDCVerifier.verify` has exactly two refusal types, and both doors (`authenticate`, `optional_subject`) are built
around them: `UnauthenticatedError` when the PRESENTED TOKEN is at fault (401, audited `invalid_token`), and
`ServiceUnavailableError` when the IdP, or this deployment's view of it, is at fault (503, audited
`verifier_unavailable`). Any third type leaves both doors as an unmapped 500, and their `except Exception` branch
records it as `invalid_token` against the caller.

Measured 2026-09-25 on pyjwt 2.13.0 / httpx 0.28.1 / pydantic 2.13, four paths reach the door as a third type
unless `oidc.py` classifies them:

* a discovery document that is not JSON (a proxy's sign-in page) — `json.JSONDecodeError` out of `response.json()`;
* a key set that is not JSON — `json.JSONDecodeError` out of `PyJWKClient.fetch_data`, which classifies only
  transport failures;
* key material `cryptography` refuses — a bare `ValueError`, because `PyJWKSet` skips only keys failing with
  `PyJWTError`;
* a token whose `alg` names another key family than the key its `kid` selects — `TypeError` out of PyJWT's
  `prepare_key` when `jwt.decode` is handed the raw key rather than the `PyJWK` that binds its algorithm. This is the
  one a CALLER chooses, so it must be a 401.

The key-set cases also cover the failures PyJWT does type (`PyJWKClientConnectionError`, `PyJWKClientError`,
`PyJWKSetError`): none of them reads the token either, so they are the provider's too, and a 401 for them would tell a
caller holding a good bearer that it is bad — the same misattribution
`test_an_unreachable_idp_is_not_the_callers_fault.py` refuses for the discovery document.

THE IdP IS A REAL HTTP SERVER ON LOOPBACK. Neither httpx nor urllib is patched, so both fetches run the libraries'
own parsing — which is where every one of these failures is raised. A stubbed response object would replace exactly
the code under test.
"""

import base64
import json
import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from service_kit.exceptions import register_handlers
from service_kit.governed.audit import AUDIT_LOGGER
from service_kit.governed.deps import make_auth_deps
from service_kit.governed.oidc import OIDCVerifier
from service_kit.lakehouse.ns_errors import install_problem_handlers


AUDIENCE = "rask"
KID = "signing-1"
DOORS = ["/gated", "/soft"]
DISCOVERY_PATH = "/.well-known/openid-configuration"
JWKS_PATH = "/jwks"

#: What a reverse proxy in front of an IdP answers when its session is gone: a 200 whose body is a page.
SIGN_IN_PAGE = b"<!doctype html><title>Sign in</title>"


class _AuthSettings(BaseModel):
    oidc_enabled: bool = True
    fga_enabled: bool = False


def _auth_settings() -> _AuthSettings:
    return _AuthSettings()


# Module scope: `make_auth_deps` must be bound where FastAPI can resolve the route annotations
# (`test_auth_deps_resolve.py` records why a local binding silently degrades to a query parameter).
_deps = make_auth_deps(Annotated[_AuthSettings, Depends(_auth_settings)])
_Subject = Annotated[str, Depends(_deps.current_subject)]
_OptionalSubject = Annotated[str, Depends(_deps.optional_subject)]


class _IdP(BaseModel):
    """What the loopback IdP serves, by path; a case overwrites one document to break it."""

    issuer: str
    documents: dict[str, bytes]


def _b64url_uint(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


def _discovery(issuer: str, *, jwks_path: str = JWKS_PATH) -> bytes:
    """Both key families advertised, as Keycloak does and as an IdP advertising none implies.

    Both matter: `verify` intersects the advertised algorithms with its allowlist, so an IdP advertising only RS256
    would refuse an ES256 header before any key is prepared, and the family-mismatch case could not arise.
    """
    return json.dumps({"issuer": issuer, "jwks_uri": f"{issuer}{jwks_path}", "id_token_signing_alg_values_supported": ["RS256", "ES256"]}).encode()


def _key_set(key: rsa.RSAPrivateKey, kid: str) -> bytes:
    public = key.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256", "n": _b64url_uint(public.n), "e": _b64url_uint(public.e)}
    return json.dumps({"keys": [jwk]}).encode()


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def stranger_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idp(rsa_key: rsa.RSAPrivateKey) -> Iterator[_IdP]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = served.documents.get(self.path)
            self.send_response(404 if body is None else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib's own parameter name
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    issuer = f"http://127.0.0.1:{server.server_address[1]}"
    served = _IdP(issuer=issuer, documents={DISCOVERY_PATH: _discovery(issuer), JWKS_PATH: _key_set(rsa_key, KID)})
    # `shutdown()` waits out one poll of `serve_forever`; at the 0.5 s default that is most of this file's runtime.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        yield served
    finally:
        server.shutdown()
        server.server_close()


def _token(idp: _IdP, key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey, algorithm: str, *, kid: str | None = KID) -> str:
    now = int(time.time())
    claims = {"iss": idp.issuer, "sub": "gina", "aud": AUDIENCE, "iat": now, "exp": now + 300}
    return jwt.encode(claims, key, algorithm=algorithm, headers={} if kid is None else {"kid": kid})


def _client(idp: _IdP) -> TestClient:
    """A governed app wired as every governed service wires one: BOTH handler installers, one verifier."""
    app = FastAPI()
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.state.oidc = OIDCVerifier(idp.issuer, AUDIENCE, cache_ttl=300, allow_insecure=True)

    @app.get("/gated")
    def _gated(subject: _Subject) -> dict[str, str]:
        return {"subject": subject}

    @app.get("/soft")
    def _soft(subject: _OptionalSubject) -> dict[str, str]:
        return {"subject": subject}

    return TestClient(app, raise_server_exceptions=False)


def _call(idp: _IdP, door: str, token: str, *, client: TestClient | None = None) -> tuple[int, str]:
    response = (client or _client(idp)).get(door, headers={"Authorization": f"Bearer {token}"})
    return response.status_code, response.text


@pytest.fixture
def audit_trail(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    return caplog


def _audited_reasons(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [getattr(record, "audit.reason", None) for record in caplog.records if record.name == AUDIT_LOGGER]


# ── the controls: the fix must not blur the line between the two refusals ─────────────────────────────


@pytest.mark.parametrize("door", DOORS)
def test_a_token_the_key_set_signed_is_accepted(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey) -> None:
    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 200, body
    assert json.loads(body) == {"subject": "gina"}


@pytest.mark.parametrize("door", DOORS)
def test_a_key_rotated_in_after_the_cached_set_is_found_by_one_refetch(
    door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, stranger_key: rsa.RSAPrivateKey
) -> None:
    """The first request caches the key set; the provider then rotates to a key that set does not hold. Refusing that
    token from the cache would lock every caller out for the cache lifetime after each rotation."""
    client = _client(idp)
    assert _call(idp, door, _token(idp, rsa_key, "RS256"), client=client)[0] == 200

    idp.documents[JWKS_PATH] = _key_set(stranger_key, "signing-2")
    status, body = _call(idp, door, _token(idp, stranger_key, "RS256", kid="signing-2"), client=client)

    assert status == 200, body


# ── the caller's fault ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("door", DOORS)
def test_a_token_signed_by_another_key_is_the_callers(door: str, idp: _IdP, stranger_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    status, body = _call(idp, door, _token(idp, stranger_key, "RS256"))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


@pytest.mark.parametrize("kid", ["retired-key", None], ids=["unknown-kid", "no-kid"])
@pytest.mark.parametrize("door", DOORS)
def test_a_kid_the_key_set_does_not_hold_is_the_callers(
    door: str, kid: str | None, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture
) -> None:
    """The key set is fine; the token names a key it does not hold (even after the one refetch a rotation earns), or
    names none at all."""
    status, body = _call(idp, door, _token(idp, rsa_key, "RS256", kid=kid))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


@pytest.mark.parametrize("door", DOORS)
def test_a_token_whose_alg_names_another_key_family_is_the_callers(door: str, idp: _IdP, audit_trail: pytest.LogCaptureFixture) -> None:
    """ES256 in the header, the RSA key's `kid` beside it. Both values are the caller's to write, and RFC 8725 §3.1
    requires each key to be used with exactly one algorithm — so this is a refused token, not a fault."""
    status, body = _call(idp, door, _token(idp, ec.generate_private_key(ec.SECP256R1()), "ES256"))

    assert status == 401, body
    assert _audited_reasons(audit_trail) == ["invalid_token"]


# ── the IdP's fault, which is ours ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("door", DOORS)
def test_a_discovery_document_that_is_not_json_is_ours(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    idp.documents[DISCOVERY_PATH] = SIGN_IN_PAGE

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


@pytest.mark.parametrize("door", DOORS)
def test_a_key_set_url_that_answers_404_is_ours(door: str, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    """The discovery document is sound and names a key-set URL nothing serves."""
    idp.documents[DISCOVERY_PATH] = _discovery(idp.issuer, jwks_path="/nothing-here")

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]


#: Key-set bodies the provider could serve, none of which the token has any part in.
UNUSABLE_KEY_SETS = {
    "not-json": SIGN_IN_PAGE,
    "json-but-not-an-object": b"[]",
    "no-keys": json.dumps({"keys": []}).encode(),
    # A modulus of zero: well-formed JSON, a well-formed JWK, and a key `cryptography` will not build.
    "key-material-refused": json.dumps({"keys": [{"kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256", "n": "AA", "e": "AQAB"}]}).encode(),
}


@pytest.mark.parametrize("key_set", UNUSABLE_KEY_SETS.values(), ids=UNUSABLE_KEY_SETS.keys())
@pytest.mark.parametrize("door", DOORS)
def test_a_key_set_with_no_usable_key_is_ours(door: str, key_set: bytes, idp: _IdP, rsa_key: rsa.RSAPrivateKey, audit_trail: pytest.LogCaptureFixture) -> None:
    idp.documents[JWKS_PATH] = key_set

    status, body = _call(idp, door, _token(idp, rsa_key, "RS256"))

    assert status == 503, body
    assert _audited_reasons(audit_trail) == ["verifier_unavailable"]
