"""The publish transport parses each HTTP response exactly once, into a real shape (ANN-17).

Two sites in `annotator.projects.lakehouse` re-read the same body:

- `publish_token` called `response.json()` twice on the IdP's token response (`id_token` or
  `access_token` — one parse per fallback arm).
- `_HttpCreateApi.create_table` built a throwaway locally-defined class whose class body parsed the
  response again, purely so the caller could read `.version`.

A doubled parse is wasted work and a trap (a `.json()` that consumes a stream would break the second
read); a throwaway class is an untyped stand-in where a declared model belongs. These tests pin the
single parse and the typed result.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _CountingResp:
    """An httpx-response stand-in that counts how many times its body is parsed."""

    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.json_calls = 0
        self.status_code = 200
        self.text = ""

    def json(self) -> dict[str, Any]:
        self.json_calls += 1
        return dict(self._body)


class _StubClient:
    """A stand-in for `lakehouse.publish_client`, the ONE pooled retrying client the publish
    transport's direct-HTTP calls now share (ANN-14). It replaced module-level `httpx.post`, which
    built and discarded a client — and a connection — per call."""

    def __init__(self, post: Any) -> None:
        self._post = post

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._post(url, **kwargs)

    def close(self) -> None:
        return None

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False


def _token_settings() -> SimpleNamespace:
    return SimpleNamespace(
        publish_token_url="http://dex:5556/dex/token",
        publish_username="publisher@rask.internal",
        publish_client_id="lance-catalog",
        publish_client_secret="lance-catalog-secret",
        publish_secret_store="lance-secrets",
        publish_secret_key="lance",
    )


def test_the_token_mint_parses_the_idp_response_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from annotator.projects import lakehouse
    from service_kit.governed import secrets as sk_secrets

    monkeypatch.setattr(sk_secrets, "fetch_required_secrets", lambda *a, **k: {"publisher-oidc-password": "pw"})
    # `access_token` only — the arm that forced the SECOND parse when `id_token` came back empty.
    resp = _CountingResp({"access_token": "minted"})
    monkeypatch.setattr(lakehouse, "publish_client", lambda _timeout: _StubClient(lambda url, **kw: resp))

    assert lakehouse.publish_token(_token_settings()) == "minted"
    assert resp.json_calls == 1, f"the token response was parsed {resp.json_calls} times — the body must be read once into a local"


def test_the_create_result_is_a_typed_model_parsed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from annotator.projects import lakehouse
    from annotator.projects.lakehouse import CreateTableResult, _HttpCreateApi

    resp = _CountingResp({"version": 7})
    monkeypatch.setattr(lakehouse, "publish_client", lambda _timeout: _StubClient(lambda url, **kw: resp))

    result = _HttpCreateApi("http://catalog:2333").create_table("silver$t", b"A", mode="exist_ok")

    assert isinstance(result, CreateTableResult), "the create result must be the module's declared model, not a locally-defined throwaway"
    assert result.version == 7
    assert resp.json_calls == 1, f"the create response was parsed {resp.json_calls} times"
