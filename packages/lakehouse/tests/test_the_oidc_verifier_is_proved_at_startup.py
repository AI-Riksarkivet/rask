"""`OIDC verifier ready` must be a measurement, and a verifier that has its keys keeps serving them.

A READY LINE IS EARNED BY A FETCH. `OIDCVerifier(...)` performs no I/O, so a verifier merely built proves nothing about
its issuer: a pod with a wrong `RASK_OIDC_ISSUER`, a wrong `RASK_OIDC_DISCOVERY_URL` (the split-horizon override) or an
unreachable IdP would log "ready", report healthy, and show the fault only as failed user traffic.

THE REFRESH IS THE PROOF, AND IT MUST NOT GATE READINESS. `refresh()` fetches every issuer's discovery document and
key set at startup, so the misconfiguration is in the pod's log at boot with the URL that failed, and returns the
issuers that failed rather than raising, because the caller is the only thing that knows whether this deployment treats
an unreachable IdP as fatal, and none of them do: readiness gated on a downstream IdP would take every governed pod out
of its Service at once over one blip.

A VERIFIER THAT HOLDS ITS KEYS SERVES THEM (ADR 0203). A request reads the provider the refresh holds, whatever its age;
only the background refresh fetches again. A request that fetched on a cache's expiry would, through an IdP outage,
fetch for itself, hold a worker thread for up to the fetch timeout, and answer 503 although the keys had not changed.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import jwk_set_cache

from lakehouse.governed import oidc


ISSUER = "https://idp.example.test"
SECOND = "https://other.example.test"


def _ok_document(issuer: str) -> dict[str, object]:
    return {"issuer": issuer, "jwks_uri": f"{issuer}/jwks", "id_token_signing_alg_values_supported": ["RS256"]}


def _a_key_set() -> dict[str, object]:
    numbers = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key().public_numbers()

    def b64(value: int) -> str:
        return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    return {"keys": [{"kty": "RSA", "kid": "k", "use": "sig", "alg": "RS256", "n": b64(numbers.n), "e": b64(numbers.e)}]}


def test_refresh_tries_EVERY_configured_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-issuer deployment is exactly where one misconfigured entry hides: the other issuer's
    traffic works, so nothing looks wrong until a user from the broken tenant signs in."""
    seen: list[str] = []
    keys = _a_key_set()

    # THE BROKEN ONE IS FIRST, deliberately. With the healthy issuer first, a `refresh` that stopped at
    # the first failure would still try both and this test would pass against it — measured: that
    # mutation survived the original ordering.
    def _second_only(self: httpx.Client, url: str, **_kw: object) -> httpx.Response:
        seen.append(url)
        if url.startswith(SECOND):
            return httpx.Response(200, json=_ok_document(SECOND), request=httpx.Request("GET", url))
        raise httpx.ConnectError("connection refused")

    def _keys(client: jwt.PyJWKClient) -> object:
        seen.append(client.uri)
        return keys

    monkeypatch.setattr(httpx.Client, "get", _second_only)
    monkeypatch.setattr(oidc._CredentialedJWKClient, "fetch_data", _keys)

    failures = oidc.OIDCVerifier([ISSUER, SECOND], "rask", 300).refresh()

    assert [issuer for issuer, _reason in failures] == [ISSUER]
    assert seen == [f"{ISSUER}/.well-known/openid-configuration", f"{SECOND}/.well-known/openid-configuration", f"{SECOND}/jwks"]


@pytest.fixture
def clock() -> Iterator[list[float]]:
    yield [1_000.0]


class _Later:
    """The clock PyJWT times its key-set cache by (`jwt.jwk_set_cache.time`), ``seconds`` ahead of the real one."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds

    def monotonic(self) -> float:
        return time.monotonic() + self._seconds


def test_a_refreshed_verifier_serves_its_keys_through_an_issuer_outage_past_its_ttl(
    dex_issuer: Any,  # the root conftest's loopback Dex
    clock: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = oidc.OIDCVerifier(dex_issuer.issuer, "rask", 300, allow_insecure=True, clock=lambda: clock[0])
    assert verifier.refresh() == []
    fetched: list[str] = []

    def _down(*_args: object, **_kw: object) -> object:
        fetched.append("a fetch")
        raise httpx.ConnectError("the identity provider is down")

    monkeypatch.setattr(httpx.Client, "get", _down)
    monkeypatch.setattr(oidc._CredentialedJWKClient, "fetch_data", _down)
    clock[0] += 10 * 300
    monkeypatch.setattr(jwk_set_cache, "time", _Later(10 * 300))

    claims = verifier.verify(dex_issuer.mint("alice", connector="local", audience="rask"))

    assert (claims.iss, fetched) == (dex_issuer.issuer, [])
