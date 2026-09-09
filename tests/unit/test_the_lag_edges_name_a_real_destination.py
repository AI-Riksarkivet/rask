"""Every declared lane's destination is rendered, so no lag edge is labelled `->?`.

`declared_edges` names an edge `<from>-><to>`, reading `to` from `MEDALLION_LANE_DESTINATIONS`. The
first deploy rendered `MEDALLION_TRANSFORM_ROUTES` (three source namespaces) and NOT that map, so every
edge would have been `bronze->?` — and `consumed_reader` splits on `->` and looks for lineage runs whose
outputs mention the destination, so `?` matches nothing. The detector would have run cleanly, published
points labelled `?`, and measured nothing: wired and inert, the shape this estate keeps paying for.

DERIVED FROM THE STAGE RUNNER DECLARATIONS, never a second list. `medallion.stageRunners[]` already states
`fromNamespace` and `toNamespace` for every lane, and `mediaStageRunners[]` does the same — so a lane added
there is measured with no second edit, and a lane renamed cannot half-move. A hand-kept map would be a
duplicate of the one declaration that already exists, which is how the edge and the stage runner drift apart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.test_invariants import _helm_template


def _producer_env() -> dict[str, str]:
    docs = [d for d in yaml.load_all(_helm_template("medallion.enabled=true", "dapr.enabled=true"), Loader=FAST_LOADER) if d]
    producer = next(d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("-medallion-producer"))
    return {e["name"]: e.get("value", "") for c in producer["spec"]["template"]["spec"]["containers"] for e in (c.get("env") or [])}


def test_the_destinations_map_is_rendered() -> None:
    env = _producer_env()
    assert "MEDALLION_LANE_DESTINATIONS" in env, (
        "no lane->destination map reaches the producer, so every lag edge is labelled `<source>->?` and "
        "matches no lineage run — the detector runs cleanly and measures nothing"
    )
    assert json.loads(env["MEDALLION_LANE_DESTINATIONS"]), "the map is empty"


def test_every_ROUTED_source_has_a_destination() -> None:
    """The two maps must agree: `transform_routes` decides which edges are measured, and a source in
    it with no destination is exactly the `->?` edge this module exists to refuse."""
    env = _producer_env()
    routed = set(json.loads(env["MEDALLION_TRANSFORM_ROUTES"]))
    destinations = json.loads(env["MEDALLION_LANE_DESTINATIONS"])
    missing = sorted(routed - set(destinations))
    assert not missing, f"these routed lanes have no declared destination: {missing}"


def test_the_destinations_come_from_the_STAGE_RUNNER_declarations() -> None:
    """Not a hand-kept second list. Each value must be some stage runner's `toNamespace`, so a renamed lane
    cannot half-move."""
    env = _producer_env()
    destinations = json.loads(env["MEDALLION_LANE_DESTINATIONS"])
    docs = [d for d in yaml.load_all(_helm_template("medallion.enabled=true", "dapr.enabled=true"), Loader=FAST_LOADER) if d]
    declared_to = {
        e["value"]
        for d in docs
        if d.get("kind") == "Deployment"
        for c in d["spec"]["template"]["spec"]["containers"]
        for e in (c.get("env") or [])
        if e.get("name") == "MEDALLION_TO_NAMESPACE"
    }
    assert declared_to, "no stage runner declares MEDALLION_TO_NAMESPACE — this gate would pass vacuously"
    stray = sorted(set(destinations.values()) - declared_to)
    assert not stray, f"these destinations match no stage runner's toNamespace: {stray}"


def test_both_lag_readers_SEND_THE_SERVICE_CREDENTIAL(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare `httpx.get` cannot read an authenticated estate, and the gauge cannot say so.

    MEASURED LIVE 2026-09-04, which is the only way this was ever going to surface: both readers sent
    no headers at all, so the published read answered **401 on every edge**. The lag detector's own
    `known=False` path then published NOTHING and reported nothing wrong — a detector that is
    silently blind is worse than an absent one, because the empty series reads as a healthy cascade.

    Asserted over the REQUEST the reader actually makes, not over a helper: a helper that returns the
    right dict proves nothing if the call site forgets to pass it, which is exactly what happened.
    """
    import httpx

    from medallion.core.config import MedallionSettings
    from medallion.services import cascade_lag_readers as readers

    settings = MedallionSettings.model_validate(
        {
            "catalog_url": "http://catalog:2333",
            "train_lineage_url": "http://lineage:8000",
            "app_api_token": "tok",
            "catalog_service_identity": "service-medallion-producer",
            # The lane's TABLES, without which the reader refuses before it builds a request — the
            # credential is only observable on a call the reader is willing to make.
            "lane_sources": {"bronze": "bronze$events"},
            "lane_destination_datasets": {"bronze": "silver$features"},
        }
    )
    seen: list[dict[str, str]] = []

    def _capture(url: str, **kwargs: Any) -> httpx.Response:
        headers: Mapping[str, str] = kwargs.get("headers") or {}
        seen.append({k.lower(): v for k, v in headers.items()})
        return httpx.Response(200, json={"tags": {}, "runs": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(readers.httpx, "get", _capture)
    readers.published_reader(settings)("bronze->silver", "acme")
    readers.consumed_reader(settings)("bronze->silver", "acme")

    assert len(seen) == 2, "both readers must make a request"
    for headers in seen:
        assert headers.get("dapr-api-token") == "tok", f"a reader sent no app token: {headers}"
        assert headers.get("x-lance-service-identity") == "service-medallion-producer", f"a reader sent no identity: {headers}"


def test_the_consumed_reader_asks_for_a_route_lineage_ACTUALLY_SERVES() -> None:
    """A route is not a thing to derive from a prefix convention — it is a thing to read off the router.

    Two routes-that-do-not-exist have shipped from this module, which is why the rule is stated rather
    than assumed. This asserts it against LINEAGE'S OWN ROUTER instead of against a literal: the
    previous version pinned the string `/runs`, and when the reader correctly stopped scanning the run
    board — a point read at `/datasets/{name}/producers` replaced a scan of a board that had just been
    bounded to 200 rows, so the reader was silently answering from a truncated list — the test failed
    for the improvement. A literal cannot tell "the route moved" from "the route is wrong"; the router
    can, and it is the thing the reader must actually agree with.
    """
    import re
    from pathlib import Path

    from lineage.main import app as lineage_app
    from medallion.services import cascade_lag_readers as readers

    # The URL EXPRESSION, not the whole file: a dead spelling may be named in a comment beside the live
    # one, and a file-wide grep would either miss the defect or refuse the explanation.
    source = Path(readers.__file__).read_text(encoding="utf-8")
    expressions = re.findall(r'url = f"(\{str\(settings\.train_lineage_url\)[^"]*)"', source)
    assert expressions, "no lineage URL found in the reader — the extraction regex has drifted from the source"

    # Reduce an f-string to the route SHAPE: drop the base-url expression (it ends at the first `}`),
    # then collapse every remaining interpolation — `{quote(wanted, safe='')}` interpolates a dataset
    # NAME — to the same placeholder the router's own `{name}` reduces to.
    def _shape(text: str) -> str:
        return re.sub(r"\{[^}]*\}", "{}", text.split("}", 1)[1])

    # `app.openapi()`, NOT `app.routes`: this service builds its routers at app-construction time under
    # a factory, and the module-level object exposes only `/dapr/subscribe` and `/ui` as plain routes —
    # asserting against that set passes nothing and would read as "lineage serves no routes at all".
    # The committed-contract test reads the same surface for the same reason.
    served = {_shape("{}" + path) for path in lineage_app.openapi()["paths"]}
    for expression in expressions:
        shape = _shape(expression)
        assert shape in served, f"the reader asks lineage for {shape!r}, which its OpenAPI does not serve"

    all_urls = re.findall(r'url = f"\{[^"]*?\}(/[a-z0-9/_{}]*)"', source)
    assert not [u for u in all_urls if u.startswith("/api/")], f"lineage serves no /api prefix; found {all_urls}"
