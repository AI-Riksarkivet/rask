"""The health badge stays 200 for everyone; the corpus facts inside it do not.

The one route exempted from the viewer's deny-by-default corpus gate, with this file as the
exemption's other half. `/api/health`'s recorded contract is ALWAYS 200 — the 2026-07-28 red-dot
regression is what happens when it isn't — so gating it like its siblings would 401 the probe and
recreate that failure in a new costume. Authorization is SOFT instead: `corpus_facts_visible` never
raises for an anonymous caller (`optional_subject` — soft only on ABSENCE; a presented-but-invalid
token still raises), and the handler REDACTS the db facts while still reporting encoder
reachability, which names no corpus.

Also the enforcement test for the decorator gate itself, driven through a REAL gated route
(`/api/columns`) — the structural walk in `test_every_corpus_route_is_gated` proves the dependency
is wired; this proves the dependency, when it runs with FGA on, actually refuses.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from viewer.api import security
from viewer.api.v1.endpoints import system
from viewer.core.config import ViewerSettings, get_viewer_settings

from service_kit.exceptions import register_handlers
from service_kit.media.deps import get_state


class _Search:
    row_table = "chunks"


class _Declared:
    document = None
    search = _Search()
    #: `columns` walks the alignments binding, which reads the declared capabilities map.
    capabilities: dict[str, Any] = {}


class _Info:
    row_count = 7
    version = 3
    columns: list[Any] = []


class _Descriptor:
    declared = _Declared()
    tables = {"chunks": _Info()}


class _Handle:
    id = "vasa"
    uri = "s3://lake/vasa"
    descriptor = _Descriptor()


class _FailingHttp:
    """Encoder pings must FAIL fast, not hang on a real socket."""

    def get(self, *_a: Any, **_k: Any) -> Any:
        raise ConnectionError("no encoder in a unit test")


class _State:
    settings = type("S", (), {"embed_url": "http://embed.invalid", "rerank_url": "http://rerank.invalid"})()
    http = _FailingHttp()


def _app(*, fga_enabled: bool, allow: bool, subject: str = "eve", monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # BOTH names: the gate reads `security._media_state.dataset_handle`, the handler bound its own
    # `dataset_handle` at import — patching one leaves the other resolving a fake state for real.
    monkeypatch.setattr(security._media_state, "dataset_handle", lambda *_a, **_k: _Handle())
    monkeypatch.setattr(system, "dataset_handle", lambda *_a, **_k: _Handle())

    app = FastAPI()
    app.include_router(system.router)
    register_handlers(app)

    settings = ViewerSettings.model_validate(
        {
            "LANCE_FGA_ENABLED": fga_enabled,
            "LANCE_OIDC_ENABLED": fga_enabled,
            "LANCE_OIDC_ISSUER": "https://issuer.test",
            "LANCE_OIDC_AUDIENCE": "rask",
        }
    )
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    app.dependency_overrides[get_state] = lambda: cast("Any", _State())
    app.dependency_overrides[security._deps.current_subject] = lambda: subject
    app.dependency_overrides[security._deps.optional_subject] = lambda: subject

    async def _checker(*, user: str, relation: str, obj: str) -> bool:
        return allow

    app.dependency_overrides[security._deps.get_checker] = lambda: _checker
    return TestClient(app)


def test_the_badge_answers_200_and_REDACTS_for_the_unentitled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(fga_enabled=True, allow=False, monkeypatch=monkeypatch)
    response = client.get("/api/health")
    assert response.status_code == 200, f"the ALWAYS-200 probe answered {response.status_code} — the red-dot regression is back"
    body = response.json()
    assert body["db"] is None, f"an unentitled caller still received the corpus facts: {body['db']}"
    assert "authorized" in (body.get("db_error") or ""), body
    assert body["embed"] is not None, "encoder reachability was redacted too — it names no corpus"


def test_the_badge_serves_facts_to_the_entitled(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _app(fga_enabled=True, allow=True, monkeypatch=monkeypatch).get("/api/health").json()
    assert body["db"] is not None and body["db"]["chunks"] == 7, body


def test_the_badge_is_open_with_fga_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev stays byte-identical."""
    body = _app(fga_enabled=False, allow=False, monkeypatch=monkeypatch).get("/api/health").json()
    assert body["db"] is not None, "FGA off must not redact anything"


def test_a_gated_sibling_route_actually_REFUSES(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decorator gate, enforced end-to-end: same corpus, no grant, 403 with the naming detail."""
    client = _app(fga_enabled=True, allow=False, monkeypatch=monkeypatch)
    response = client.get("/api/columns")
    assert response.status_code == 403, f"/api/columns answered {response.status_code} for a caller with no grant"
    assert "eve lacks can_get_metadata" in response.text, response.text


def test_a_granted_caller_passes_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _app(fga_enabled=True, allow=True, monkeypatch=monkeypatch).get("/api/columns").status_code == 200
