"""The lag readers call routes the catalog and lineage actually serve.

The first version of `published_reader` invented `GET /v1/table/{id}/publication`. The catalog serves no
such route — its publication router exposes `POST /{id}/publish` and nothing else — so every edge failed
on the first live tick with `cascade_lag_edge_unreadable`. The per-edge containment did its job (the
tick completed and published nothing rather than lying), which is exactly why the failure was visible
as a warning rather than as a silent zero.

A unit test cannot prove a URL exists on a running service, but it CAN prove the reader asks for the
route this repo declares — which is what would have caught an invented path before it shipped.

`GET /v1/table/{id}/tags/list` is the real door (`endpoints/tags.py`, the spec's ListTableTags with a
GET compat alias), and its response is `{"tags": {"<name>": {"version": N, ...}}}` — verified against
the installed `lance_namespace_urllib3_client` models rather than assumed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from medallion.services.cascade_lag_readers import consumed_reader, published_reader


class _Settings:
    catalog_url = "http://catalog:2333"
    train_lineage_url = "http://lineage:8000"
    transform_routes: dict[str, str] = {}
    lane_destinations: dict[str, str] = {}
    lag_projects: list[str] = []


def _capture(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], status: int = 200) -> list[str]:
    seen: list[str] = []

    def _get(url: str, **_: object) -> httpx.Response:
        seen.append(url)
        return httpx.Response(status, content=json.dumps(payload), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _get)
    return seen


def test_the_published_reader_asks_the_TAGS_route(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, {"tags": {"published": {"version": 7}}})
    assert published_reader(_Settings())("bronze->silver", "acme") == 7
    assert seen and seen[0].endswith("/tags/list"), seen


def test_a_table_with_no_published_tag_reads_None(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never published — an idle, healthy edge, which `lag_for_edge` reads as lag 0."""
    _capture(monkeypatch, {"tags": {"stable": {"version": 3}}})
    assert published_reader(_Settings())("bronze->silver", "acme") is None


def test_a_missing_table_reads_None_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, {}, status=404)
    assert published_reader(_Settings())("bronze->silver", "acme") is None


def test_a_catalog_error_RAISES_so_the_tick_counts_it_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 is not "no lag". Raising is what lets `run_lag_tick` count the edge FAILED and publish
    nothing, instead of a zero that reads as perfect health."""
    _capture(monkeypatch, {}, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        published_reader(_Settings())("bronze->silver", "acme")


def test_the_consumed_reader_asks_the_RUNS_board(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, {"runs": [{"outputs": ["silver$features"], "consumed_to_version": 5}]})
    assert consumed_reader(_Settings())("bronze->silver", "acme") == 5
    assert seen and seen[0].endswith("/runs"), seen
