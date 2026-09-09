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

import pytest


def test_the_projection_is_NOT_governed_per_dataset() -> None:
    """The whole point: the per-dataset filter that makes `/events` right for a person is exactly what
    makes it wrong for a reconciler, so the projection must not apply it."""
    from lineage.api.v1.endpoints import runs

    source = inspect.getsource(runs.get_events_projection)
    assert "governed(" not in source, "the projection re-applies the per-dataset filter it exists to omit"
    assert "list_events" in source, "the projection does not read the durable feed"


def test_the_projection_is_gated_on_the_ESTATE_rung() -> None:
    """`can_observe_events` on the root object — the same rung `POST /v1/projects` gates on, so an
    estate privilege means one thing everywhere rather than one thing per service. It is `owner` on the
    root in `model.fga`, so nobody holds it by accident."""
    from lineage.api.v1.endpoints import runs

    source = inspect.getsource(runs.get_events_projection)
    assert "require_estate_observer" in source, "the projection is ungoverned AND ungated — that is a disclosure hole, not a fix"


def test_the_gate_checks_the_ROOT_object_not_a_table() -> None:
    """`_require_relation` composes `<fga_object_type>:<name>`, which on this service is always
    `table:`. An estate privilege is not a relation of any one table, so the gate must read
    `fga_root_object` verbatim — checking `table:<something>` would ask a question the model cannot
    answer and deny every caller."""
    from lineage.api import fga_deps

    source = inspect.getsource(fga_deps.require_estate_observer)
    assert "fga_root_object" in source, "the estate gate does not check the root object"
    assert "can_observe_events" in source
    # The DOCSTRING names `fga_object_type` to explain why it is not used, so assert against the CODE
    # with the prose stripped — a gate test that a comment can satisfy or break is testing the comment.
    # Split at the docstring's terminator rather than subtracting `getdoc`, which dedents and therefore
    # never matches the indented source.
    body = source.split('"""', 2)[-1]
    assert "fga_object_type" not in body, "the estate gate composes a table object — an estate rung is not a table's"


def test_the_gate_FAILS_CLOSED_the_same_way_every_other_gate_does() -> None:
    """An unwired client is a 503 and an unauthenticated caller a 401 — never an allow. A gate that
    opens on its own outage is worse than no gate, because the estate believes it is protected."""
    from lineage.api import fga_deps

    source = inspect.getsource(fga_deps.require_estate_observer)
    assert "ServiceUnavailableError" in source, "an unwired authorization client would fall through"
    assert "UnauthenticatedError" in source, "an unauthenticated caller would fall through"
    deny = source.index("PermissionDeniedError")
    check = source.index("fga.check")
    assert check < deny, "the denial is raised without consulting OpenFGA"


@pytest.mark.parametrize("route", ["/events/projection", "/events"])
def test_BOTH_doors_are_mounted_and_the_governed_one_is_unchanged(route: str) -> None:
    """The projection is additive. `/events` keeps its per-dataset filter, because a person reading the
    board is exactly the caller that filter is right for."""
    from lineage.api.v1.endpoints.runs import router

    paths = {r.path for r in router.routes}  # ty: ignore[unresolved-attribute]
    assert route in paths, f"{route} is not mounted: {sorted(paths)}"

    if route == "/events":
        from lineage.api.v1.endpoints import runs

        assert "governed(" in inspect.getsource(runs.get_events), "the governed feed lost its filter"
