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

from medallion.core.config import MedallionSettings
from medallion.services.cascade_lag import ConsumedRange, EdgeNotMeasurable
from medallion.services.cascade_lag_readers import consumed_reader, published_reader


class _Settings:
    """The settings surface the readers touch — including the CREDENTIAL fields.

    Their absence was not neutral: the readers built no headers, every test passed, and the deployed gauge
    answered 401 on every edge. A double that omits what the code under test reads does not simplify the
    test, it hides a class of defect from it. The token files are the real settings' own, which the
    suite's autouse fixture points at one file per door.
    """

    catalog_url = "http://catalog:2333"
    train_lineage_url = "http://lineage:8000"
    transform_routes: dict[str, str] = {}
    lane_destinations: dict[str, str] = {"bronze": "silver", "bronze-media": "silver-media"}
    #: The SOURCE and DESTINATION tables of each lane, project-unqualified — the shape
    #: `chart/templates/medallion.yaml` derives from `stageRunners[].fromDataset` / `.toDataset`.
    lane_sources: dict[str, str] = {"bronze": "bronze$events", "bronze-media": "bronze-media$objects"}
    lane_destination_datasets: dict[str, str] = {"bronze": "silver$features", "bronze-media": "silver-media$features"}
    lag_projects: list[str] = []

    def __init__(self) -> None:
        real = MedallionSettings()
        self.catalog_identity_token_file = real.catalog_identity_token_file
        self.lineage_identity_token_file = real.lineage_identity_token_file


def _capture(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], status: int = 200) -> list[str]:
    seen: list[str] = []

    def _get(url: str, **_: object) -> httpx.Response:
        seen.append(url)
        return httpx.Response(status, content=json.dumps(payload), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _get)
    return seen


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


def test_the_published_reader_asks_for_a_TABLE_THAT_EXISTS(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect that made this detector blind: it asked for the source NAMESPACE as if it were a table.

    `<project>-<source namespace>` is `acme-bronze`, and no writer creates a table by that name — the
    lane's table is `acme-bronze$events`. `fga_deps.require_parent` refuses a single-segment table id at
    create, so `/v1/table/acme-bronze` can name no table that has ever existed: every tick 404'd, the
    reader mapped that to "nothing published", and a cascade that had never run once reported lag 0.

    The name comes from the stage runner's own `fromDataset` declaration, project-qualified by the same helper
    the rest of the estate uses, so a renamed lane cannot half-move.
    """
    seen = _capture(monkeypatch, {"tags": {"published": {"version": 7}}})
    published_reader(_Settings())("bronze->silver", "acme")
    assert "/v1/table/acme-bronze%24events/tags/list" in seen[0] or "/v1/table/acme-bronze$events/tags/list" in seen[0], seen


def test_the_consumed_reader_MATCHES_ONE_TENANT_not_every_lookalike(monkeypatch: pytest.MonkeyPatch) -> None:
    """`any(destination in str(output) ...)` was a SUBSTRING test over an unqualified namespace, and
    `(edge, project)` is the gauge's key — so the project never reached the query at all.

    `silver` is a substring of `acme-silver$features`, `beta-silver$features` AND
    `silver-media$features`. Measured against the real reader, one tenant's edge read another tenant's
    consumed version and a fan-out lane's as its own: with acme at 3 and beta at 9, acme reported 9 —
    ahead of its own source, which `lag_for_edge` then reports UNKNOWN, so the edge went dark.

    THE ISOLATION NOW LIVES IN THE QUERY rather than in a filter this reader applies, so this asserts
    the URL: `/datasets/{name}/producers` matches `{name:$name}` exactly, and a request that named the
    bare namespace would be answered about a different dataset. Naming the wrong thing is the failure
    mode that survives moving the match server-side.
    """
    seen = _capture(monkeypatch, {"dataset": "acme-silver$features", "producers": [{"consumed_from_version": None, "consumed_to_version": 3}]})

    assert consumed_reader(_Settings())("bronze->silver", "acme") == [ConsumedRange(from_version=None, to_version=3)]
    asked = seen[0]
    assert asked.endswith("/producers")
    assert "acme-silver%24features/producers" in asked or "acme-silver$features/producers" in asked, (
        f"the reader asked about {asked!r} — a project-qualified dataset, not a bare namespace, is what keeps one tenant's lag out of another's"
    )
    assert "beta" not in asked


def test_a_dataset_LINEAGE_cannot_show_us_is_unmeasurable_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same rule as the published reader's, on the reader thirty lines below it that did not hold it.

    MEASURED ON THE LIVE ESTATE 2026-09-10 by driving every declared edge: of 261, 245 were invisible
    (silent, correct) and 15 read fine — and exactly ONE failed, every tick, forever:

        ('silver->gold', 'advref31') -> 403 Forbidden
        GET /datasets/advref31-gold$catalog/producers

    `published_reader` translates that condition; this one called `raise_for_status()` bare, so the
    identical refusal was `unmeasurable` on one side of the file and a WARNING plus a `failed` count on
    the other. A repeating-condition warning is the noise this module's docstring already cites: the
    `failed` counter is supposed to mean something is broken, and a permanent entry in it is how a real
    failure arrives unnoticed.

    Lineage collapses the two meanings exactly as the catalog does — probed live, an unknown dataset
    and a forbidden one BOTH answer 403 — so "not visible to this subject" is the honest reading of
    either, and neither `None` (a fabricated healthy 0) nor a raise is.
    """
    _capture(monkeypatch, {}, status=403)
    with pytest.raises(EdgeNotMeasurable):
        consumed_reader(_Settings())("bronze->silver", "acme")


def test_a_lineage_error_still_RAISES_so_the_tick_counts_it_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative half, and it is why this is a translation rather than a blanket catch: a 500 IS a
    fault, and swallowing it would turn a broken lineage service into a silently idle cascade."""
    _capture(monkeypatch, {}, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        consumed_reader(_Settings())("bronze->silver", "acme")
