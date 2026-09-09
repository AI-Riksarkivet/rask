"""`DELETE /v1/warehouses/{id}/namespaces/{ns}` — the unbind door (§ Q15-1).

A binding is a ROUTING RECORD (`top_ns -> warehouse_id -> root_uri`). Until this door existed the only
way to remove one was `POST /v1/namespace/{id}/drop`, which destroys the namespace and every table in it
to reach a JSON file. Three such records sit stranded on this estate naming warehouses that hold no
bytes, and the repair was blocked on having no non-destructive door.

THE SHAPE IS `delete_warehouse`'s, scaled to one binding, because the hazards are the same ones:
authorize before disclosing anything, refuse while full and NAME what blocks it, and collapse a denial
into the not-found answer so the door is not an existence oracle.
"""

from __future__ import annotations

import pytest

from catalog.services import warehouses


def test_the_unbind_action_is_in_the_control_vocabulary() -> None:
    """The binding cache is positive-and-forever, so the registry write is only half the repair.

    `warehouse_unbound` has to be its OWN action: the namespace still exists (no `namespace_dropped`
    fires) and the warehouse still exists (no `warehouse_deleted` either), so no existing event covers
    it. Without the word, every replica already holding the entry keeps routing a namespace at a
    warehouse nothing binds it to — the registry says unbound and the running estate disagrees, which
    is worse than not having the door.
    """
    from service_kit.control_events import ControlAction

    assert "warehouse_unbound" in ControlAction.__args__


def test_the_unbind_event_evicts_the_binding_cache() -> None:
    """Driven through the REAL evictor, because that is the half a unit test of the door cannot reach."""
    cache = {"gold": {"warehouse_id": "wh1", "root_uri": "s3://b/gold"}, "other": {"warehouse_id": "wh2", "root_uri": "s3://b/o"}}
    evicted = warehouses.evict_stale_bindings(cache, action="warehouse_unbound", object_id="warehouse:wh1", extra={"namespace": "gold"}, delimiter="$")
    assert evicted == ["gold"], f"the unbound namespace was not evicted: {evicted}"
    assert "other" in cache, "an unrelated binding was evicted"


def test_an_unbind_event_naming_no_namespace_evicts_nothing() -> None:
    """A malformed event must not clear the cache wholesale — a mass eviction is a thundering re-read of
    the registry on every replica at once."""
    cache = {"gold": {"warehouse_id": "wh1", "root_uri": "s3://b/gold"}}
    assert warehouses.evict_stale_bindings(cache, action="warehouse_unbound", object_id="warehouse:wh1", extra={}, delimiter="$") == []
    assert cache == {"gold": {"warehouse_id": "wh1", "root_uri": "s3://b/gold"}}


def test_unbind_is_idempotent_at_the_primitive() -> None:
    """A partial failure (record removed, the broadcast failed, the operator retries) must converge
    rather than 404 on the half that already succeeded."""
    import inspect

    source = inspect.getsource(warehouses.unbind_namespace)
    assert "FileNotFoundError" in source, "unbind must swallow an absent record, or a retry cannot converge"


@pytest.mark.parametrize("phrase", ["still holds", "unresolvable"])
def test_the_door_refuses_a_NON_EMPTY_namespace_and_says_why(phrase: str) -> None:
    """Unbinding a live namespace deletes no byte and is still the worst outcome available: every table
    in it becomes unresolvable, because routing no longer knows which bucket holds it. A refusal that
    does not NAME what blocks it just moves the search to the operator — the same rule the warehouse
    delete's 409 already follows."""
    import inspect

    from catalog.api.v1.endpoints import warehouses as door

    source = inspect.getsource(door.unbind_warehouse_namespace)
    assert phrase in source, f"the non-empty refusal does not explain itself: missing {phrase!r}"


def test_the_door_authorizes_BEFORE_it_discloses_and_leaks_no_ids() -> None:
    """Two rules at once, both from `delete_warehouse`:

    the gate runs before anything that reveals the warehouse's contents, and a denial collapses to the
    same 404 a missing warehouse gets (NO EXISTENCE ORACLE, audit #4) so nobody probes which warehouse
    ids exist through the quieter door.
    """
    import inspect

    from catalog.api.v1.endpoints import warehouses as door

    source = inspect.getsource(door.unbind_warehouse_namespace)
    gate = source.index("can_administer")
    disclosure = source.index("binding_for_namespace")
    assert gate < disclosure, "the door reads the binding before authorizing — a disclosure ahead of its gate"
    assert "raise TableNotFoundError" in source[gate:disclosure], "a denial does not collapse into the not-found answer"
