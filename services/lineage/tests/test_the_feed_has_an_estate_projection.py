"""The feed's estate projection — a service can see what it must reconcile (§ G1).

MEASURED BEFORE IT WAS BUILT, on the deployed estate 2026-09-09. Driven as `service-ingest`, which
holds `can_get_metadata` on `table:acme-gold$catalog` = False, against a run the graph demonstrably
holds:

    GET /runs/42d5180d-…                      -> 404      (the run exists; the caller is told it does not)
    GET /runs/42d5180d-…/inputs               -> 200 []   (governed-drop: an empty list, not a refusal)
    GET /datasets/acme-gold$catalog/producers -> 403

Both failure shapes are SILENT. A reconciler walking that runs cleanly and reconciles nothing, which is
indistinguishable from an estate with no work to do — so the lane whose whole job is catching what the
bus missed cannot report that it missed anything.

THE SERVICE IS NOT THE DISCLOSURE BOUNDARY. A reconciler reads the feed to decide who to TELL, and the
telling is gated per subject at delivery (`can_be_notified`). Filtering the reader's own view protects
nobody and guarantees it cannot find what it exists to catch.
"""

from __future__ import annotations

import inspect


def test_the_projection_is_gated_on_the_ESTATE_rung() -> None:
    """`can_observe_events` on the root object — the same rung `POST /v1/projects` gates on, so an
    estate privilege means one thing everywhere rather than one thing per service. It is `owner` on the
    root in `model.fga`, so nobody holds it by accident."""
    from lineage.api.v1.endpoints import runs

    source = inspect.getsource(runs.get_events_projection)
    assert "require_estate_observer" in source, "the projection is ungoverned AND ungated — that is a disclosure hole, not a fix"
