"""A REFUSED provenance read must not read as "no defect".

THE FALSE NEGATIVE, measured 2026-08-05 while lineage's OIDC door was armed:

    ingest pod:  4x "401 Client Error: Unauthorized for url:
                 http://rask-lineage:8000/api/v1/lineage"   in five minutes
    the SAME run: {"status": "COMPLETE", ..., "defect": null}
    the lane:     "OK  A8 — no provenance defect"

A8 certified provenance as sound while EVERY lineage event was being rejected. The mechanism was one
bare `except Exception`: the read was sent with no credentials, the graph answered 401,
`raise_for_status()` raised, the handler returned `None` for "could not ask", and the endpoint maps
`None` to no-defect on purpose (an unanswerable question must not be reported as a defect).

The reasoning behind that mapping is right. The gap is that a 401 is NOT an unanswerable question —
the graph is reachable and telling us we may not look. That is a misconfiguration, and it makes this
service's A8 verdict untrustworthy, which an operator has to be told.

A gate that passes when the thing it guards is entirely broken is worse than no gate.
"""

from __future__ import annotations

import httpx
import pytest
from ingest.provenance import LineageProvenanceReader, ProvenanceRefused, _service_headers


@pytest.mark.parametrize("status_code", [401, 403])
def test_a_REFUSED_read_raises_rather_than_looking_like_an_outage(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    """The heart of it. 401 and 403 both mean "reachable, and refusing" — never "unreachable"."""

    def _refuse(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "Missing bearer token"}, request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.get", _refuse)

    with pytest.raises(ProvenanceRefused):
        LineageProvenanceReader().has_run("some-run")


def test_an_UNREACHABLE_graph_still_returns_None(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original behaviour, deliberately unchanged: a genuinely unanswerable question is None, and
    the endpoint reports no defect for it. Claiming a defect from ignorance is how the check loses its
    meaning — that reasoning was never wrong, it just did not cover refusal."""

    def _explode(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("httpx.get", _explode)

    assert LineageProvenanceReader().has_run("some-run") is None


def test_a_5xx_is_an_outage_NOT_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary. A 500 from the graph is the graph being broken, which is exactly the case the
    None branch is for — only the AUTHORIZATION statuses change meaning."""

    def _boom(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(500, text="kaboom", request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.get", _boom)

    assert LineageProvenanceReader().has_run("some-run") is None


def test_a_present_run_is_still_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path must keep working — and it exercises the id translation, since the graph keys
    on the LINEAGE run id, not the ingest one."""
    from ingest.lineage import lineage_run_id

    target = lineage_run_id("run-42")

    def _ok(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"runs": [{"run_id": target}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.get", _ok)

    assert LineageProvenanceReader().has_run("run-42") is True


def test_an_ABSENT_run_is_False_not_None(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "The graph answered and does not contain it" is the REAL defect A8 exists to catch, and it must
    stay distinguishable from every flavour of "we could not tell"."""

    def _empty(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"runs": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.get", _empty)

    assert LineageProvenanceReader().has_run("run-42") is False


# ── the credentials, which is why the read was refused in the first place ────


def test_the_read_SENDS_the_service_credential_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The read and the WRITE go through the same lineage door, so the reader needs the same pair the
    emitter does. Sending none is what made a refusal indistinguishable from an outage."""
    monkeypatch.setenv("RASK_LINEAGE_APP_TOKEN", "tok")
    monkeypatch.setenv("RASK_LINEAGE_SERVICE_IDENTITY", "service-ingest")

    headers = _service_headers()

    assert headers["dapr-api-token"] == "tok"
    assert headers["x-lance-service-identity"] == "service-ingest"


def test_a_HALF_configured_credential_sends_NOTHING(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lineage door needs BOTH. Sending one produces a request that is refused for a reason
    invisible from here — the emitter warns about exactly this pair
    ("lineage_service_door_half_configured"), and a matching silence on the read side would repeat
    the defect. Sending nothing at least makes the refusal unambiguous."""
    monkeypatch.setenv("RASK_LINEAGE_APP_TOKEN", "tok")
    monkeypatch.delenv("RASK_LINEAGE_SERVICE_IDENTITY", raising=False)

    assert _service_headers() == {}

    monkeypatch.delenv("RASK_LINEAGE_APP_TOKEN", raising=False)
    monkeypatch.setenv("RASK_LINEAGE_SERVICE_IDENTITY", "service-ingest")

    assert _service_headers() == {}


def test_APP_API_TOKEN_is_accepted_as_the_fallback_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chart injects the estate's app token as `APP_API_TOKEN`; the emitter accepts either name.
    Accepting only the lineage-specific one here would mean the reader and the emitter disagree about
    what counts as configured — the exact half-configured state the pair check exists to refuse."""
    monkeypatch.delenv("RASK_LINEAGE_APP_TOKEN", raising=False)
    monkeypatch.setenv("APP_API_TOKEN", "shared")
    monkeypatch.setenv("RASK_LINEAGE_SERVICE_IDENTITY", "service-ingest")

    assert _service_headers()["dapr-api-token"] == "shared"
