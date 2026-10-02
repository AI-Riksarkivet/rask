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

from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from ingest.lineage import lineage_run_id
from ingest.provenance import LineageProvenanceReader, ProvenanceRefused


if TYPE_CHECKING:
    import pathlib


@pytest.mark.parametrize("status_code", [401, 403])
def test_a_REFUSED_read_raises_rather_than_looking_like_an_outage(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    """The heart of it. 401 and 403 both mean "reachable, and refusing" — never "unreachable"."""

    def _refuse(_client: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "Missing bearer token"}, request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.Client.get", _refuse)

    with pytest.raises(ProvenanceRefused):
        LineageProvenanceReader().has_run("some-run")


def test_a_5xx_is_an_outage_NOT_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary. A 500 from the graph is the graph being broken, which is exactly the case the
    None branch is for — only the AUTHORIZATION statuses change meaning."""

    def _boom(_client: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(500, text="kaboom", request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.Client.get", _boom)

    assert LineageProvenanceReader().has_run("some-run") is None


# ── the credential: this pod's projected rask-lineage token ([[LH-220]], D1) ────


@respx.mock
def test_the_read_carries_the_lineage_identity_token(sa_issuer: Any, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The read and the WRITE go through the same lineage door, so the reader presents the same identity the emitter does."""
    token = sa_issuer.mint("rask-sa-ingest", audience="rask-lineage")
    token_file = tmp_path / "rask-lineage-token"
    token_file.write_text(token)
    monkeypatch.setenv("RASK_LINEAGE_IDENTITY_TOKEN_FILE", str(token_file))
    route = respx.get(f"http://rask-lineage:8000/runs/{lineage_run_id('some-run')}").mock(return_value=httpx.Response(404))

    assert LineageProvenanceReader().has_run("some-run") is False

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {token}"


@respx.mock
def test_no_identity_token_is_a_refusal_not_an_outage(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pod with no token to present cannot be answered by the graph at all: its A8 verdict is untrustworthy, which
    an operator must be told, and "could not ask" would render as no defect. Nothing is sent without it."""
    monkeypatch.setenv("RASK_LINEAGE_IDENTITY_TOKEN_FILE", str(tmp_path / "absent"))

    with pytest.raises(ProvenanceRefused, match="no lineage identity token"):
        LineageProvenanceReader().has_run("some-run")
