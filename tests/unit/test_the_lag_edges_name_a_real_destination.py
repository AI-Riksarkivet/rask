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


def test_every_ROUTED_source_has_a_destination() -> None:
    """The two maps must agree: `transform_routes` decides which edges are measured, and a source in
    it with no destination is exactly the `->?` edge this module exists to refuse."""
    env = _producer_env()
    routed = set(json.loads(env["MEDALLION_TRANSFORM_ROUTES"]))
    destinations = json.loads(env["MEDALLION_LANE_DESTINATIONS"])
    missing = sorted(routed - set(destinations))
    assert not missing, f"these routed lanes have no declared destination: {missing}"


def test_each_lag_reader_SENDS_THE_TOKEN_PROJECTED_FOR_THE_DOOR_IT_CALLS(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare `httpx.get` cannot read an authenticated estate, and the gauge cannot say so.

    MEASURED LIVE 2026-09-04, which is the only way this was ever going to surface: both readers sent
    no headers at all, so the published read answered **401 on every edge**. The lag detector's own
    `known=False` path then published NOTHING and reported nothing wrong — a detector that is
    silently blind is worse than an absent one, because the empty series reads as a healthy cascade.

    A service is the account its projected token names ([[LH-220]]), and the catalog and lineage verify
    different audiences, so each reader presents the token projected for ITS door and nothing else names
    the caller. Asserted over the REQUEST the reader actually makes, not over a helper: a helper that
    returns the right dict proves nothing if the call site forgets to pass it, which is exactly what happened.
    """
    from pathlib import Path

    import httpx

    from medallion.core.config import MedallionSettings
    from medallion.services import cascade_lag_readers as readers

    settings = MedallionSettings.model_validate(
        {
            "catalog_url": "http://catalog:2333",
            "train_lineage_url": "http://lineage:8000",
            # The lane's TABLES, without which the reader refuses before it builds a request — the
            # credential is only observable on a call the reader is willing to make.
            "lane_sources": {"bronze": "bronze$events"},
            "lane_destination_datasets": {"bronze": "silver$features"},
        }
    )
    seen: dict[str, dict[str, str]] = {}

    def _capture(url: str, **kwargs: Any) -> httpx.Response:
        headers: Mapping[str, str] = kwargs.get("headers") or {}
        seen[url] = {k.lower(): v for k, v in headers.items()}
        return httpx.Response(200, json={"tags": {}, "runs": []}, request=httpx.Request("GET", url))

    monkeypatch.setattr(readers.httpx, "get", _capture)
    readers.published_reader(settings)("bronze->silver", "acme")
    readers.consumed_reader(settings)("bronze->silver", "acme")

    assert len(seen) == 2, "both readers must make a request"
    catalog_call, lineage_call = seen.values()
    assert catalog_call == {"authorization": f"Bearer {Path(settings.catalog_identity_token_file).read_text()}"}
    assert lineage_call == {"authorization": f"Bearer {Path(settings.lineage_identity_token_file).read_text()}"}
