"""Encoder construction must be direct, not shaped by test doubles (VS-19).

open_python-audit VS-19 — `clients._construct` picked the kwargs to pass by
`inspect.signature(factory)`, so that a param-less fake monkeypatched over the client
class would "work" by silently receiving NOTHING. That put the production construction
path in service of test doubles: a fake with the wrong signature proved nothing (it
never saw `embed_url`), and a real client whose signature drifted would be constructed
with silently-dropped kwargs instead of failing loudly.

Pinned here: construction passes the real kwargs unconditionally. A fake that cannot
accept them TypeErrors (surfaced as the accessor's 503) instead of being quietly
accommodated — which is exactly the coupling's absence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from search.services import clients
from search.services.encoders import embedding, reranker

from service_kit.exceptions import ServiceUnavailableError


if TYPE_CHECKING:
    from service_kit.media.state import AppState


class _Settings:
    embed_url = "http://embed.invalid"
    rerank_url = "http://rerank.invalid"


class _State:
    def __init__(self) -> None:
        self.settings = _Settings()
        self.embedder = None
        self.reranker = None


def test_a_paramless_fake_is_not_silently_accommodated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The introspection branch existed FOR this fake; without it, the fake must fail loudly."""

    class _ParamlessFake:
        def __init__(self) -> None: ...

    monkeypatch.setattr(embedding, "VLLMEmbeddingClient", _ParamlessFake)
    monkeypatch.setattr(clients, "_default_embed_dim", lambda _s: 128)

    with pytest.raises(ServiceUnavailableError):
        clients.ensure_embedder(cast("AppState", _State()))


def test_the_embedder_is_constructed_with_the_real_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Fake:
        def __init__(self, embed_url: str, *, expected_dim: int) -> None:
            seen["embed_url"] = embed_url
            seen["expected_dim"] = expected_dim

    monkeypatch.setattr(embedding, "VLLMEmbeddingClient", _Fake)
    monkeypatch.setattr(clients, "_default_embed_dim", lambda _s: 128)

    state = _State()
    built = clients.ensure_embedder(cast("AppState", state))

    assert seen == {"embed_url": "http://embed.invalid", "expected_dim": 128}
    assert state.embedder is built, "the accessor must cache the client on the state"


def test_the_reranker_is_constructed_with_the_real_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Fake:
        def __init__(self, rerank_url: str) -> None:
            seen["rerank_url"] = rerank_url

    monkeypatch.setattr(reranker, "VLLMReranker", _Fake)

    state = _State()
    built = clients.ensure_reranker(cast("AppState", state))

    assert seen == {"rerank_url": "http://rerank.invalid"}
    assert state.reranker is built


def test_the_introspection_seam_is_gone() -> None:
    """`_construct` was the coupling itself — its absence is part of the contract."""
    assert not hasattr(clients, "_construct"), "clients._construct still exists — construction is still signature-introspected"
    import inspect as _inspect  # noqa: PLC0415 — asserting on the MODULE's imports, not using inspect

    source = _inspect.getsource(clients)
    assert "inspect.signature" not in source, "clients still introspects a factory signature"
