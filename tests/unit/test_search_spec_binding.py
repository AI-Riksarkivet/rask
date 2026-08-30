"""GET /api/search binds its spec from the query string EXPLICITLY, never via a FastAPI query-model.

`Annotated[SearchSpec, Query()]` is broken on the deployed FastAPI (0.140.13): under a real uvicorn
socket the model never flattens — the server demands one required `spec` param and 422s every flat
call, which killed live search estate-wide — while TestClient binds the identical request happily,
so the in-process suite stayed green through the whole outage. Proven by driving the same app object
through both paths inside the running pod (2026-08-07). These tests pin the explicit binding and the
tripwire against the pattern coming back; no TestClient is used, because TestClient is the liar here.
"""

from __future__ import annotations

import inspect
from typing import cast

import pytest

from search.api.v1 import router as router_module
from search.api.v1.router import _spec_from_query
from service_kit.exceptions import ValidationError


class _Req:
    """Just the one attribute the binder reads."""

    def __init__(self, params: dict[str, str]) -> None:
        self.query_params = params


def test_flat_params_bind_with_coercion() -> None:
    spec = _spec_from_query(cast("router_module.Request", _Req({"q": "vasa", "n": "5", "mode": "fts", "phrase": "true"})))
    assert (spec.q, spec.n, spec.mode, spec.phrase) == ("vasa", 5, "fts", True)


def test_filter_and_corpus_params_are_ignored_not_rejected() -> None:
    """Filters and `corpus` ride the same query string; extra="ignore" must keep dropping them."""
    spec = _spec_from_query(cast("router_module.Request", _Req({"q": "x", "topic": "ships", "corpus": "a"})))
    assert spec.q == "x"


def test_type_error_is_the_clean_domain_400() -> None:
    with pytest.raises(ValidationError):
        _spec_from_query(cast("router_module.Request", _Req({"n": "not-a-number"})))


def test_the_broken_query_model_pattern_stays_out() -> None:
    """Tripwire: the pattern that 422'd live search must not return to this handler.

    The invariant is the SIGNATURE: `search_get` takes no `spec` parameter — the moment one
    reappears, FastAPI owns the binding again and the broken query-model path is back on the wire.
    """
    assert "spec" not in inspect.signature(router_module.search_get).parameters
