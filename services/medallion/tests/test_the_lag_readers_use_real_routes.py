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

from medallion.services.cascade_lag import ConsumedRange, EdgeNotMeasurable
from medallion.services.cascade_lag_readers import consumed_reader, published_reader


class _Settings:
    """The settings surface the readers touch — including the CREDENTIAL fields.

    Those three were absent, and their absence was not neutral: the readers built no headers, every
    test passed, and the deployed gauge answered 401 on every edge. A double that omits what the code
    under test reads does not simplify the test, it hides a class of defect from it.

    `secrets_from_dapr = False` is the dev shape: no secret store, so the shared-token path applies,
    which is what `dedicated_token_for` returns `None` for.
    """

    catalog_url = "http://catalog:2333"
    train_lineage_url = "http://lineage:8000"
    transform_routes: dict[str, str] = {}
    lane_destinations: dict[str, str] = {"bronze": "silver", "bronze-media": "silver-media"}
    #: The SOURCE and DESTINATION tables of each lane, project-unqualified — the shape
    #: `chart/templates/medallion.yaml` derives from `movers[].fromDataset` / `.toDataset`.
    lane_sources: dict[str, str] = {"bronze": "bronze$events", "bronze-media": "bronze-media$objects"}
    lane_destination_datasets: dict[str, str] = {"bronze": "silver$features", "bronze-media": "silver-media$features"}
    lag_projects: list[str] = []
    app_api_token = "shared-token"
    catalog_service_identity = "service-medallion-producer"
    secrets_from_dapr = False


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


@pytest.mark.parametrize("status", [403, 404])
def test_a_table_this_subject_CANNOT_SEE_is_unmeasurable_not_idle(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """The catalog collapses "not yours" and "does not exist" into ONE answer, deliberately — a
    destructive door that distinguished them would be an id-enumeration oracle
    (`rask-lance-catalog`, "NO EXISTENCE ORACLE"). The gate also runs BEFORE existence resolution, so
    an absent table answers 403 rather than 404 and the detector cannot tell the two apart.

    It must therefore claim NEITHER. `None` would mean "never published", which `lag_for_edge` reads as
    a healthy idle edge and publishes as a confident 0 — measured live 2026-09-05, that would have put
    a fabricated lag-0 series on 255 edges belonging to abandoned test projects. Raising would count
    every one of them FAILED on every tick forever, which is the repeating-condition noise this
    module's own docstring cites row 23 for.
    """
    _capture(monkeypatch, {}, status=status)
    with pytest.raises(EdgeNotMeasurable):
        published_reader(_Settings())("bronze->silver", "acme")


def test_a_catalog_error_RAISES_so_the_tick_counts_it_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 is not "no lag". Raising is what lets `run_lag_tick` count the edge FAILED and publish
    nothing, instead of a zero that reads as perfect health."""
    _capture(monkeypatch, {}, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        published_reader(_Settings())("bronze->silver", "acme")


def test_the_consumed_reader_asks_the_RUNS_board(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch, {"runs": [{"outputs": ["acme-silver$features"], "consumed_to_version": 5, "consumed_from_version": 2}]})
    assert consumed_reader(_Settings())("bronze->silver", "acme") == [ConsumedRange(from_version=2, to_version=5)]
    assert seen and seen[0].endswith("/runs"), seen


def test_the_published_reader_asks_for_a_TABLE_THAT_EXISTS(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect that made this detector blind: it asked for the source NAMESPACE as if it were a table.

    `<project>-<source namespace>` is `acme-bronze`, and no writer creates a table by that name — the
    lane's table is `acme-bronze$events`. `fga_deps.require_parent` refuses a single-segment table id at
    create, so `/v1/table/acme-bronze` can name no table that has ever existed: every tick 404'd, the
    reader mapped that to "nothing published", and a cascade that had never run once reported lag 0.

    The name comes from the mover's own `fromDataset` declaration, project-qualified by the same helper
    the rest of the estate uses, so a renamed lane cannot half-move.
    """
    seen = _capture(monkeypatch, {"tags": {"published": {"version": 7}}})
    published_reader(_Settings())("bronze->silver", "acme")
    assert "/v1/table/acme-bronze%24events/tags/list" in seen[0] or "/v1/table/acme-bronze$events/tags/list" in seen[0], seen


def test_a_single_tenant_estate_asks_for_the_UNQUALIFIED_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """`project_namespace("", name)` returns the name unchanged, which is the whole single-tenant
    contract — a leading `-` would name nothing."""
    seen = _capture(monkeypatch, {"tags": {"published": {"version": 7}}})
    published_reader(_Settings())("bronze->silver", "")
    assert "acme" not in seen[0] and ("bronze%24events" in seen[0] or "bronze$events" in seen[0]), seen


def test_the_consumed_reader_MATCHES_ONE_TENANT_not_every_lookalike(monkeypatch: pytest.MonkeyPatch) -> None:
    """`any(destination in str(output) ...)` was a SUBSTRING test over an unqualified namespace, and
    `(edge, project)` is the gauge's key — so the project never reached the query at all.

    `silver` is a substring of `acme-silver$features`, `beta-silver$features` AND
    `silver-media$features`. Measured against the real reader, one tenant's edge read another tenant's
    consumed version and a fan-out lane's as its own: with acme at 3 and beta at 9, acme reported 9 —
    ahead of its own source, which `lag_for_edge` then reports UNKNOWN, so the edge went dark.
    """
    _capture(
        monkeypatch,
        {"runs": [
            {"outputs": ["acme-silver$features"], "consumed_from_version": None, "consumed_to_version": 3},
            {"outputs": ["beta-silver$features"], "consumed_from_version": None, "consumed_to_version": 9},
            {"outputs": ["acme-silver-media$features"], "consumed_from_version": None, "consumed_to_version": 42},
        ]},
    )
    assert consumed_reader(_Settings())("bronze->silver", "acme") == [ConsumedRange(from_version=None, to_version=3)]


def test_the_consumed_reader_returns_EVERY_range_not_just_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap between two ranges is the loss, so a reader that reduced to `max()` threw away the only
    evidence of it before the detector ever saw the numbers."""
    _capture(
        monkeypatch,
        {"runs": [
            {"outputs": ["acme-silver$features"], "consumed_from_version": None, "consumed_to_version": 3},
            {"outputs": ["acme-silver$features"], "consumed_from_version": 5, "consumed_to_version": 8},
        ]},
    )
    assert consumed_reader(_Settings())("bronze->silver", "acme") == [
        ConsumedRange(from_version=None, to_version=3),
        ConsumedRange(from_version=5, to_version=8),
    ]


def test_a_run_with_no_ceiling_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A promotion or a full rescan carries no range. It consumed no stated delta, so it is evidence of
    nothing here — and a `to_version` of 0 borrowed for it would claim coverage it never had."""
    _capture(monkeypatch, {"runs": [{"outputs": ["acme-silver$features"], "consumed_to_version": None}]})
    assert consumed_reader(_Settings())("bronze->silver", "acme") == []
