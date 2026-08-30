"""ANN-14 — the publish transport's two DIRECT-HTTP calls must pool and retry like its SDK half.

`CatalogPublisher` speaks to the catalog over two stacks. The tag calls go through the generated
client, built as `ApiClient(Configuration(host=..., retries=3))` — urllib3 retries a dropped
connection there. The other two calls are ours: the IdP token mint and the S4 `create` (direct HTTP
because the generated client cannot send `source`/`source_version`). Both were bare
`httpx.post(...)` module calls, which build and discard a `Client` — and therefore a connection —
per invocation, with `retries` at httpx's default of 0.

So the publish had one half that survived a reset connection and one half that did not, and the half
that did not is the one that WRITES: a create that dies on a transient connect failure fires
`publish_failed` and leaves the project waiting for a watchdog tick.

`create` is safe to retry by construction — it posts `mode=exist_ok`, and the module docstring's
retry-safety argument rests on exactly that.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

import service_kit.governed.secrets as secrets_module
from annotator.core.config import AnnotatorSettings
from annotator.projects import lakehouse


def _refuse_bare_post(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the publish transport called `httpx.post`, which builds and discards a client (and a connection) per call")


def _mock_client_factory(recorded: list[httpx.Request], made: list[httpx.Client], body: dict[str, Any]) -> Any:
    """A stand-in for the pooled factory that answers `body` and records every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json=body)

    def factory(timeout: float) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)
        made.append(client)
        return client

    return factory


def test_the_pooled_client_retries_a_dropped_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory exists, and its transport carries the same retry budget as the SDK half."""
    assert hasattr(lakehouse, "publish_client"), "there is no shared publish client — each call builds its own, with retries at httpx's default of 0"

    seen: list[Any] = []

    class _Recording(httpx.HTTPTransport):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs.get("retries"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", _Recording)
    lakehouse.publish_client(30.0).close()

    assert lakehouse.CATALOG_TRANSPORT_RETRIES > 0
    assert seen == [lakehouse.CATALOG_TRANSPORT_RETRIES]


def test_the_create_call_reuses_one_client_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """One `_HttpCreateApi` holds one pooled client — not one per `create_table`."""
    monkeypatch.setattr(httpx, "post", _refuse_bare_post)
    recorded: list[httpx.Request] = []
    made: list[httpx.Client] = []
    monkeypatch.setattr(lakehouse, "publish_client", _mock_client_factory(recorded, made, {"version": 7}), raising=False)

    api = lakehouse._HttpCreateApi("http://catalog.example")
    try:
        assert api.create_table("ns.one", b"rows").version == 7
        assert api.create_table("ns.two", b"rows").version == 7
    finally:
        api.close()

    assert len(recorded) == 2
    assert len(made) == 1, "a fresh client (and connection) per create call"


def test_the_token_mint_goes_through_the_same_pooled_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The IdP mint is the transport's other direct-HTTP call and had the same defect."""
    monkeypatch.setattr(httpx, "post", _refuse_bare_post)
    monkeypatch.setattr(secrets_module, "fetch_required_secrets", lambda *_a, **_k: {"publisher-oidc-password": "shh"})
    recorded: list[httpx.Request] = []
    made: list[httpx.Client] = []
    monkeypatch.setattr(lakehouse, "publish_client", _mock_client_factory(recorded, made, {"id_token": "minted"}), raising=False)

    settings = AnnotatorSettings()
    monkeypatch.setattr(settings, "publish_token_url", "https://idp.example/token", raising=False)
    monkeypatch.setattr(settings, "publish_username", "publisher", raising=False)

    assert lakehouse.publish_token(settings) == "minted"
    assert len(recorded) == 1
    assert len(made) == 1
