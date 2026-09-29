"""An IdP the estate cannot reach is OUR fault, and must be answered and audited as ours.

Found by an external comparison (`antoniocali/polaris-k8s`, 2026-09-20), which proves its auth plane
at startup and surfaces the verdict on a condition rather than discovering it on the first request.
That half is a separate question; this one is the defect underneath it.

THREE CONFIGURATION FAULTS WERE CHARGED TO THE CALLER. `OIDCVerifier.verify` maps only
``(jwt.PyJWTError, jwt.PyJWKClientError, ValidationError)`` to `UnauthenticatedError`, and the
discovery fetch does not happen inside that mapping at all — `_provider_for` resolves (and therefore
fetches) BEFORE the try block. So an unreachable issuer (`httpx.ConnectError`), a discovery path that
answers non-2xx (`raise_for_status`) and a discovery document whose `issuer` disagrees with the
configured one all escape as something the door never classified.

WHAT THAT PRODUCED, on both doors (`authenticate` and `optional_subject`), whose handler is
``except Exception: audit("authn", FAILURE, reason="invalid_token"); raise``:

  * the AUDIT TRAIL records `invalid_token` against a real subject — the estate's evidence of who was
    refused and why, asserting that a caller presented a bad bearer when the bearer was never read;
  * the CALLER gets a 500, because an `httpx` error is not a `DomainError` and no handler maps it.

Both are wrong in the same direction: an outage in the estate's own configuration is reported as the
user's mistake, which hides the outage and slanders the user. N failed requests during an IdP blip
write N false `invalid_token` records.

THE VOCABULARY ALREADY EXISTED ONE BRANCH ABOVE. `deps.py` audits a MISSING verifier as
`verifier_unavailable` and answers `ServiceUnavailableError` (503). A verifier that exists but cannot
reach its issuer is the same fact arriving later, so it gets the same reason and the same status —
this adds no vocabulary, it stops one path from bypassing it.

THE ISSUER MISMATCH GOES WITH THEM, and that is the one worth arguing. It reads like a security
refusal ("defends against a tampered discovery doc") and it is — but it is a statement about the
DISCOVERY DOCUMENT versus this deployment's configuration, and the presented token plays no part in
it. Answering 401 tells a caller their credential is bad when it may be perfectly good, and the real
condition is that this service cannot currently trust its own issuer. `_provider_for`'s
"Unrecognized token issuer" stays a 401: that one IS about the token.
"""

from __future__ import annotations

import httpx
import pytest

from service_kit.governed import oidc


ISSUER = "https://idp.example.test"


def _verifier(issuer: str | list[str] = ISSUER, *, allow_insecure: bool = False) -> oidc.OIDCVerifier:
    return oidc.OIDCVerifier(issuer=issuer, audience="rask", cache_ttl=300, allow_insecure=allow_insecure)


def _unverified_token() -> str:
    """A token whose ISSUER claim resolves, so `_provider_for` proceeds to discovery.

    Deliberately not a valid signed token: every case here must fail during discovery, BEFORE any
    signature is checked, which is exactly the window the defect lived in.
    """
    import jwt

    return jwt.encode({"iss": ISSUER, "sub": "u1", "aud": "rask", "exp": 9999999999, "iat": 1}, "secret", algorithm="HS256")


def _discovery_document(monkeypatch: pytest.MonkeyPatch, **document: object) -> None:
    def _serve(self: httpx.Client, url: str, **_kw: object) -> httpx.Response:
        return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks", **document}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", _serve)


def test_a_discovery_document_advertising_an_HTTP_key_set_is_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key-set URL is the provider's to advertise, and the scheme check reads nothing else."""
    _discovery_document(monkeypatch, jwks_uri="http://idp.example.test/jwks")

    with pytest.raises(oidc.ProviderUnavailableError):
        _verifier().verify(_unverified_token())


@pytest.mark.parametrize(
    ("jwks_uri", "allow_insecure"),
    [
        pytest.param("https://[::1/jwks", False, id="https://[::1/jwks-https-only"),
        pytest.param("https://[::1/jwks", True, id="https://[::1/jwks-allow-insecure"),
        pytest.param("file:///etc/hostname", True, id="file:///etc/hostname-allow-insecure"),
    ],
)
def test_a_discovery_document_advertising_a_key_set_url_no_client_can_fetch_is_service_unavailable(
    monkeypatch: pytest.MonkeyPatch, jwks_uri: str, allow_insecure: bool
) -> None:
    """A URL that does not parse, or whose scheme is not HTTP, is the provider's whatever `allow_insecure` says."""
    _discovery_document(monkeypatch, jwks_uri=jwks_uri)

    with pytest.raises(oidc.ProviderUnavailableError):
        _verifier(allow_insecure=allow_insecure).verify(_unverified_token())
