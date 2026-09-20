"""`OIDC verifier ready` must be a measurement, not an assertion.

Found by the same external comparison as the 503 fix beside it
(`docs/audits/2026-09-20-polaris-k8s-comparison.md`): the upstream operator resolves its auth plane
once at startup and carries the verdict, rather than discovering it on the first request.

WHAT THE LINE CLAIMED. `attach_governed_auth` constructs `OIDCVerifier(...)` and logs
``"%s: OIDC verifier ready (issuer=%s)"``. The constructor performs no I/O at all — it normalises the
issuer list and stores settings. Discovery and JWKS are fetched lazily by `_resolve`, on the first
request that presents a bearer. So a pod with a wrong `RASK_OIDC_ISSUER`, a wrong
`RASK_OIDC_DISCOVERY_URL` (the split-horizon override whose omission at ONE of six doors once broke
every signed-in ingest) or an unreachable IdP comes up logging "ready", reports healthy, and the
fault appears only as failed user traffic.

Its `except` compounded the claim: "OIDC verifier failed to build — governed routes will 503" covers
a CONSTRUCTION failure, and the only construction failure the constructor can raise is an empty
issuer list, which `GovernedAuthSettings` already refuses. The branch could not fire for the reason
it named.

WARMING IS THE PROOF, AND IT MUST NOT GATE READINESS. Two things are being separated here:

  * warm at startup, so the misconfiguration is in the pod's log at boot with the URL that failed,
    and so the first real request does not pay the discovery round-trip;
  * do NOT fail startup or `/readyz` on it. Readiness that gates on a downstream dependency turns one
    IdP blip into every governed pod leaving its Service endpoints at once — a self-inflicted estate
    outage strictly worse than the honest 503 the door now answers. The pod stays up and says so.

So: warming reports, it does not decide. `warm()` returns the issuers that failed and their reason
rather than raising, because the caller is the only thing that knows whether this deployment treats
an unreachable IdP as fatal, and today none of them do.
"""

from __future__ import annotations

import httpx
import pytest

from service_kit.governed import oidc


ISSUER = "https://idp.example.test"
SECOND = "https://other.example.test"


def _ok_document(issuer: str) -> dict[str, object]:
    return {"issuer": issuer, "jwks_uri": f"{issuer}/jwks", "id_token_signing_alg_values_supported": ["RS256"]}


def test_warm_reports_a_reachable_issuer_as_no_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: a healthy IdP warms clean, so a non-empty result below means something."""

    def _ok(self: httpx.Client, url: str, **_kw: object) -> httpx.Response:
        return httpx.Response(200, json=_ok_document(ISSUER), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", _ok)

    assert oidc.OIDCVerifier(ISSUER, "rask", 300).warm() == []


def test_warm_NAMES_the_issuer_it_could_not_reach(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFECT: nothing contacted the issuer, so `ready` was asserted. The reason has to carry the
    issuer, because the split-horizon override means the URL that failed is not always the one an
    operator configured as the issuer."""

    def _refuse(self: httpx.Client, url: str, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.Client, "get", _refuse)

    failures = oidc.OIDCVerifier(ISSUER, "rask", 300).warm()

    assert [issuer for issuer, _reason in failures] == [ISSUER]
    assert "could not be reached" in failures[0][1], failures[0][1]


def test_warm_DOES_NOT_RAISE(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that keeps this from becoming an outage amplifier. Warming reports; the caller
    decides. A `warm` that raised would make every governed pod refuse to start whenever the IdP was
    briefly unreachable, which is strictly worse than the 503 the door already answers."""

    def _refuse(self: httpx.Client, url: str, **_kw: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.Client, "get", _refuse)

    oidc.OIDCVerifier(ISSUER, "rask", 300).warm()  # must not raise


def test_warm_tries_EVERY_configured_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A multi-issuer deployment is exactly where one misconfigured entry hides: the other issuer's
    traffic works, so nothing looks wrong until a user from the broken tenant signs in."""
    seen: list[str] = []

    # THE BROKEN ONE IS FIRST, deliberately. With the healthy issuer first, a `warm` that stopped at
    # the first failure would still try both and this test would pass against it — measured: that
    # mutation survived the original ordering.
    def _second_only(self: httpx.Client, url: str, **_kw: object) -> httpx.Response:
        seen.append(url)
        if url.startswith(SECOND):
            return httpx.Response(200, json=_ok_document(SECOND), request=httpx.Request("GET", url))
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.Client, "get", _second_only)

    failures = oidc.OIDCVerifier([ISSUER, SECOND], "rask", 300).warm()

    assert [issuer for issuer, _reason in failures] == [ISSUER]
    assert len(seen) == 2, f"warm stopped at the first failure and never tried the rest: {seen}"


def test_a_warmed_verifier_does_not_refetch_on_the_first_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second reason to warm. `_resolve` caches per issuer for `cache_ttl`, so warming moves the
    discovery round-trip off the first user request — a latency spike that lands on whoever happens
    to sign in after a rollout."""
    calls: list[str] = []

    def _count(self: httpx.Client, url: str, **_kw: object) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, json=_ok_document(ISSUER), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", _count)
    verifier = oidc.OIDCVerifier(ISSUER, "rask", 300)
    verifier.warm()
    before = len(calls)

    with pytest.raises(Exception):  # noqa: B017 — the token is junk; only the FETCH COUNT is under test
        verifier.verify("not-a-token")

    assert len(calls) == before, f"discovery was re-fetched after warming: {calls[before:]}"


def test_the_startup_path_WARMS_rather_than_asserting() -> None:
    """The line that made this invisible. `attach_governed_auth` logged "OIDC verifier ready" for a
    constructor that performs no I/O; without a call to `warm` there, everything above is a capability
    nothing uses."""
    import inspect

    from service_kit.governed import auth_lifespan

    source = inspect.getsource(auth_lifespan)

    # `.warm` rather than `warm(`: the call goes through `run_in_threadpool(app.state.oidc.warm)`,
    # which passes the bound method rather than calling it — matching on the parenthesis would fail
    # against the correct implementation.
    assert ".warm" in source, "the startup path still asserts readiness instead of proving it"
    assert "run_in_threadpool" in source, (
        "`warm` performs blocking HTTP and `attach_auth` is a coroutine — awaiting it inline stalls "
        "the worker, which is the reason `verify_off_loop` exists one module over"
    )
