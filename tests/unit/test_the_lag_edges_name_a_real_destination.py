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
