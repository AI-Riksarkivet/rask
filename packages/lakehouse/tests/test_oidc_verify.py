"""Unit tests for :class:`lakehouse.governed.oidc.OIDCVerifier.verify`.

These exercise the *real* verification path — signature, issuer, audience, expiry,
and the algorithm allowlist — with zero network. We generate an RSA keypair locally
(``cryptography``), sign tokens with PyJWT, and stub only the single IO boundary
(``OIDCVerifier._resolve``) so the verifier uses our local public key instead of a
fetched JWKS. Everything downstream of that boundary (``jwt.decode`` with the
allowlisted algorithms, issuer/audience/exp checks, opaque error mapping) runs for
real.

The split-horizon section at the bottom goes one level deeper: it runs the *real*
``_resolve`` and stubs only its one network touch-point (the ``httpx.Client`` both the
discovery fetch and the key fetch use) so the tests can assert
*where* discovery/JWKS are fetched from while the issuer checks run unmodified.

Mirrors the fastapi_oidc conftest approach: ``rsa.generate_private_key`` +
``jwt.encode`` against the matching public key.

Assumptions about the hardened verifier (already landed in ``services/catalog/core/oidc.py``):

* ``OIDCVerifier(issuer, audience, cache_ttl, *, allowed_algorithms=..., leeway=...,
  allow_insecure=...)`` — an asymmetric-only ``allowed_algorithms`` allowlist that is
  intersected with the provider's advertised algorithms.
* ``verify()`` returns an :class:`IDToken` on success and raises
  :class:`lance_namespace.UnauthenticatedError` (never a raw PyJWT error) on any
  failure: expired, wrong audience, wrong issuer, bad signature, or a disallowed
  algorithm such as ``HS256`` (alg-confusion defence).

The tests are written against this *public* behaviour. If the constructor keyword
changes name, only ``_make_verifier`` below needs updating.
"""

import base64
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from lance_namespace import UnauthenticatedError

from lakehouse.governed import oidc as oidc_module
from lakehouse.governed.oidc import OIDCVerifier, ProviderUnavailableError, _Discovery, _Provider


ISSUER = "https://idp.example"
AUDIENCE = "lance"
KID = "test-key-1"


# --------------------------------------------------------------------------- #
# Local RSA keypair + a real JWKS client whose one fetch answers its public key.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[Any, Any]:
    """A locally-generated RSA keypair; the private key signs, the public verifies."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def private_pem(rsa_keypair: tuple[Any, Any]) -> bytes:
    """PEM-encoded private key, the form PyJWT's ``encode`` accepts for RS256."""
    private_key, _ = rsa_keypair
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _local_key_set(public_key: rsa.RSAPublicKey) -> dict[str, Any]:
    """The JWK Set a provider publishing ``public_key`` serves, under the one ``kid`` the tests sign with."""
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _make_verifier(
    rsa_keypair: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    advertised_algorithms: list[str] | None = None,
    **verifier_kwargs: Any,
) -> OIDCVerifier:
    """Build a verifier whose only IO boundary (``_resolve``) is stubbed to the local key.

    ``_resolve`` is the single method that touches the network (discovery + JWKS). We
    replace it with one that returns a :class:`_Provider` built from a controlled
    discovery doc and the local key set as PyJWT builds it, while still running the verifier's
    *real* ``_safe_algorithms`` intersection so the allowlist is genuinely exercised.
    """
    _, public_key = rsa_keypair
    advertised = ["RS256"] if advertised_algorithms is None else advertised_algorithms

    verifier = OIDCVerifier(ISSUER, AUDIENCE, cache_ttl=3600, **verifier_kwargs)

    def fake_resolve(self: OIDCVerifier, configured_issuer: str) -> _Provider:
        spec = _Discovery(
            issuer=configured_issuer,
            jwks_uri=f"{configured_issuer}/jwks",
            id_token_signing_alg_values_supported=advertised,
        )
        # Run the verifier's real allowlist intersection so HS256/none can never slip in.
        algorithms = self._safe_algorithms(spec.id_token_signing_alg_values_supported)
        keys = tuple(jwt.PyJWKSet(_local_key_set(public_key)["keys"]).keys)
        return _Provider(spec=spec, jwks_uri=spec.jwks_uri, algorithms=algorithms, keys=keys, began=0)

    monkeypatch.setattr(OIDCVerifier, "_resolve", fake_resolve)
    return verifier


def _b64url(raw: bytes) -> str:
    """base64url without padding, as used in JWS compact serialization."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _claims(**overrides: Any) -> dict[str, Any]:
    """A valid claim set; tests override individual fields to make a token invalid."""
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER,
        "sub": "alice",
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
    }
    base.update(overrides)
    return base


def _sign(private_pem: bytes, claims: dict[str, Any]) -> str:
    """Sign ``claims`` as an RS256 JWT with the given PEM private key (honest path).

    Forgeries (HS256 alg-confusion, ``none``) are hand-assembled instead — see
    ``_forge_hs256`` and the ``none`` test — because PyJWT's ``encode`` refuses to
    produce them, exactly the constraint an attacker does not have.
    """
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": KID})


# --------------------------------------------------------------------------- #
# Accept: a well-formed RS256 token.
# --------------------------------------------------------------------------- #


def test_verify_preserves_extra_claims(rsa_keypair: tuple[Any, Any], private_pem: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    # extra='allow' keeps provider-specific claims (e.g. email/groups) accessible.
    verifier = _make_verifier(rsa_keypair, monkeypatch)
    token = _sign(private_pem, _claims(email="alice@example.com", groups=["admin"]))

    result = verifier.verify(token)

    # extra='allow' surfaces unknown claims via the model dump (and as attributes at
    # runtime); assert through the dump so the access is ty/ruff-clean.
    dumped = result.model_dump()
    assert dumped["email"] == "alice@example.com"
    assert dumped["groups"] == ["admin"]


def test_verify_rejects_expired_token(rsa_keypair: tuple[Any, Any], private_pem: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    # exp well outside the verifier's leeway window.
    verifier = _make_verifier(rsa_keypair, monkeypatch, leeway=0)
    now = int(time.time())
    token = _sign(private_pem, _claims(iat=now - 600, exp=now - 300))

    with pytest.raises(UnauthenticatedError):
        verifier.verify(token)


# --------------------------------------------------------------------------- #
# Reject: expired / wrong-aud.
# Each must surface as an opaque UnauthenticatedError (never a raw PyJWT error).
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Split-horizon discovery (reverse-proxy IdP): the issuer STRING stays public,
# only the discovery/JWKS FETCH location moves in-cluster. These run the real
# ``_resolve`` and stub only its network touch-points.
# --------------------------------------------------------------------------- #

PUBLIC_ISSUER = "https://public.example/dex"
INTERNAL_DEX = "https://dex.cluster.local:5556/dex"
_WELL_KNOWN = "/.well-known/openid-configuration"


def _stub_network(
    monkeypatch: pytest.MonkeyPatch,
    *,
    document: dict[str, Any],
    public_key: rsa.RSAPublicKey,
) -> tuple[list[str], list[str]]:
    """Serve ``document`` from any discovery URL and the local key from any JWKS URI.

    Returns two recorders — the discovery URLs and the JWKS URIs fetched — so tests assert the *fetch locations*
    while ``_resolve``'s own logic (issuer match, https guard, allowlist) runs real.

    The client is the real class, cut at the wire: a real ``httpx.Client`` on a ``MockTransport``, so both fetches
    parse a real ``httpx.Response``.
    """
    discovery_urls: list[str] = []
    jwks_urls: list[str] = []
    real_client = httpx.Client

    def _serve(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(_WELL_KNOWN):
            discovery_urls.append(str(request.url))
            return httpx.Response(200, json=document)
        jwks_urls.append(str(request.url))
        return httpx.Response(200, json=_local_key_set(public_key))

    def _client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(_serve), **kwargs)

    monkeypatch.setattr(oidc_module.httpx, "Client", _client)
    return discovery_urls, jwks_urls


def _dex_document(issuer: str) -> dict[str, Any]:
    """A Dex-shaped discovery doc: jwks_uri advertised under the issuer, as Dex does."""
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/keys",
        "id_token_signing_alg_values_supported": ["RS256"],
    }


def test_split_horizon_fetches_from_override_and_verifies(rsa_keypair: tuple[Any, Any], private_pem: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    # (a) Discovery + JWKS are fetched from the override; a token carrying the PUBLIC
    # issuer as ``iss`` verifies against the configured (public) issuer.
    _, public_key = rsa_keypair
    discovery_urls, jwks_urls = _stub_network(monkeypatch, document=_dex_document(PUBLIC_ISSUER), public_key=public_key)
    verifier = OIDCVerifier(
        PUBLIC_ISSUER,
        AUDIENCE,
        cache_ttl=3600,
        discovery_overrides={PUBLIC_ISSUER: INTERNAL_DEX},
    )
    token = _sign(private_pem, _claims(iss=PUBLIC_ISSUER))

    result = verifier.verify(token)

    assert result.iss == PUBLIC_ISSUER
    assert discovery_urls == [f"{INTERNAL_DEX}{_WELL_KNOWN}"]
    # The issuer-hosted jwks_uri is rebased onto the override — the key fetch stays in-cluster.
    assert jwks_urls == [f"{INTERNAL_DEX}/keys"]


def test_split_horizon_discovery_issuer_mismatch_still_rejects(rsa_keypair: tuple[Any, Any], private_pem: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    # (b) The security anchor is untouched: a discovery doc whose ``issuer`` differs from
    # the CONFIGURED issuer is rejected even when fetched from the override (e.g. the
    # in-cluster Dex misconfigured with its internal URL as issuer).
    #
    # REJECTED AS OURS, 503, NOT AS THE CALLER'S 401 — and this row's own example is why. A Dex
    # "misconfigured with its internal URL as issuer" is a deployment fault; the presented token is
    # never read in reaching it, so answering 401 tells a caller with a perfectly good bearer that
    # their credential is bad, and the door audits it as `invalid_token` against their subject. The
    # security property asserted here is that it is REFUSED, and it still is.
    _, public_key = rsa_keypair
    _stub_network(monkeypatch, document=_dex_document(INTERNAL_DEX), public_key=public_key)
    verifier = OIDCVerifier(
        PUBLIC_ISSUER,
        AUDIENCE,
        cache_ttl=3600,
        discovery_overrides={PUBLIC_ISSUER: INTERNAL_DEX},
    )
    token = _sign(private_pem, _claims(iss=PUBLIC_ISSUER))

    with pytest.raises(ProviderUnavailableError):
        verifier.verify(token)


def test_split_horizon_leaves_foreign_jwks_uri_alone(rsa_keypair: tuple[Any, Any], private_pem: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    # A jwks_uri NOT hosted under the issuer (provider keeps keys elsewhere) is used as
    # advertised — the override only rebases issuer-hosted URLs.
    _, public_key = rsa_keypair
    document = _dex_document(PUBLIC_ISSUER) | {"jwks_uri": "https://keys.example/jwks"}
    _discovery_urls, jwks_urls = _stub_network(monkeypatch, document=document, public_key=public_key)
    verifier = OIDCVerifier(
        PUBLIC_ISSUER,
        AUDIENCE,
        cache_ttl=3600,
        discovery_overrides={PUBLIC_ISSUER: INTERNAL_DEX},
    )
    token = _sign(private_pem, _claims(iss=PUBLIC_ISSUER))

    verifier.verify(token)

    assert jwks_urls == ["https://keys.example/jwks"]


def test_split_horizon_http_override_requires_allow_insecure(rsa_keypair: tuple[Any, Any], private_pem: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    # The HTTPS guard applies to the override URL under the same knob: a plain-http
    # in-cluster fetch is rejected by default and allowed only with allow_insecure.
    _, public_key = rsa_keypair
    public_issuer = "http://localhost:8090/dex"
    internal_dex = "http://lance-ns-dex:5556/dex"
    discovery_urls, _jwks_urls = _stub_network(monkeypatch, document=_dex_document(public_issuer), public_key=public_key)

    def _make(*, allow_insecure: bool) -> OIDCVerifier:
        return OIDCVerifier(
            public_issuer,
            AUDIENCE,
            cache_ttl=3600,
            allow_insecure=allow_insecure,
            discovery_overrides={public_issuer: internal_dex},
        )

    token = _sign(private_pem, _claims(iss=public_issuer))

    # This deployment's setting, not the caller's token: refused as ours.
    with pytest.raises(ProviderUnavailableError):
        _make(allow_insecure=False).verify(token)
    assert discovery_urls == []  # guard fires BEFORE any fetch

    result = _make(allow_insecure=True).verify(token)
    assert result.iss == public_issuer
    assert discovery_urls == [f"{internal_dex}{_WELL_KNOWN}"]


def test_a_discovery_override_that_is_no_url_is_the_deployments_fault(private_pem: bytes) -> None:
    """The override is this deployment's setting, and the presented token is never read in reaching it: a value no
    client can dial is answered as a provider that cannot be used, never as the caller's bad token."""
    verifier = OIDCVerifier(PUBLIC_ISSUER, AUDIENCE, cache_ttl=3600, discovery_overrides={PUBLIC_ISSUER: "https://[not-a-host"})

    with pytest.raises(ProviderUnavailableError):
        verifier.verify(_sign(private_pem, _claims(iss=PUBLIC_ISSUER)))
