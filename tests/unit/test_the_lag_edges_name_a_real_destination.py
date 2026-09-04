"""Every declared lane's destination is rendered, so no lag edge is labelled `->?`.

`declared_edges` names an edge `<from>-><to>`, reading `to` from `MEDALLION_LANE_DESTINATIONS`. The
first deploy rendered `MEDALLION_TRANSFORM_ROUTES` (three source namespaces) and NOT that map, so every
edge would have been `bronze->?` — and `consumed_reader` splits on `->` and looks for lineage runs whose
outputs mention the destination, so `?` matches nothing. The detector would have run cleanly, published
points labelled `?`, and measured nothing: wired and inert, the shape this estate keeps paying for.

DERIVED FROM THE MOVER DECLARATIONS, never a second list. `medallion.movers[]` already states
`fromNamespace` and `toNamespace` for every lane, and `mediaMovers[]` does the same — so a lane added
there is measured with no second edit, and a lane renamed cannot half-move. A hand-kept map would be a
duplicate of the one declaration that already exists, which is how the edge and the mover drift apart.
"""

from __future__ import annotations

import json

import yaml

from tests.unit.test_invariants import _helm_template


def _producer_env() -> dict[str, str]:
    docs = [d for d in yaml.safe_load_all(_helm_template("medallion.enabled=true", "dapr.enabled=true")) if d]
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


def test_the_destinations_come_from_the_MOVER_declarations() -> None:
    """Not a hand-kept second list. Each value must be some mover's `toNamespace`, so a renamed lane
    cannot half-move."""
    env = _producer_env()
    destinations = json.loads(env["MEDALLION_LANE_DESTINATIONS"])
    docs = [d for d in yaml.safe_load_all(_helm_template("medallion.enabled=true", "dapr.enabled=true")) if d]
    declared_to = {
        e["value"]
        for d in docs
        if d.get("kind") == "Deployment"
        for c in d["spec"]["template"]["spec"]["containers"]
        for e in (c.get("env") or [])
        if e.get("name") == "MEDALLION_TO_NAMESPACE"
    }
    assert declared_to, "no mover declares MEDALLION_TO_NAMESPACE — this gate would pass vacuously"
    stray = sorted(set(destinations.values()) - declared_to)
    assert not stray, f"these destinations match no mover's toNamespace: {stray}"


def test_both_lag_readers_SEND_THE_SERVICE_CREDENTIAL() -> None:
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
        }
    )
    seen: list[dict[str, str]] = []

    def _capture(url: str, **kwargs: object) -> httpx.Response:
        seen.append({k.lower(): v for k, v in dict(kwargs.get("headers") or {}).items()})
        return httpx.Response(200, json={"tags": {}, "runs": []}, request=httpx.Request("GET", url))

    original = readers.httpx.get
    readers.httpx.get = _capture  # type: ignore[assignment]
    try:
        readers.published_reader(settings)("bronze->silver", "acme")
        readers.consumed_reader(settings)("bronze->silver", "acme")
    finally:
        readers.httpx.get = original  # type: ignore[assignment]

    assert len(seen) == 2, "both readers must make a request"
    for headers in seen:
        assert headers.get("dapr-api-token") == "tok", f"a reader sent no app token: {headers}"
        assert headers.get("x-lance-service-identity") == "service-medallion-producer", f"a reader sent no identity: {headers}"


def test_the_consumed_reader_asks_for_a_route_lineage_ACTUALLY_SERVES() -> None:
    """Lineage mounts its run board at `/runs`, not under a version prefix.

    Probed live against the deployed service: `/v1/runs` 404, `/api/v1/runs` 404, `/runs` 401 —
    present and asking for a credential. This is the SECOND route-that-does-not-exist in this file's
    short history, so the rule is worth stating: a route is not a thing to derive from a prefix
    convention, it is a thing to read off the router.
    """
    import re
    from pathlib import Path

    from medallion.services import cascade_lag_readers as readers

    # The URL EXPRESSION, not the whole file: the dead spelling is named in a comment right beside
    # the live one, and a file-wide grep would either miss the defect or refuse the explanation.
    source = Path(readers.__file__).read_text(encoding="utf-8")
    urls = re.findall(r'url = f"\{[^"]*?\}(/[a-z0-9/_]*)"', source)
    assert "/runs" in urls, f"the consumed reader must ask for /runs; it asks for {urls}"
    assert not [u for u in urls if u.startswith("/api/")], f"lineage serves no /api prefix; found {urls}"
