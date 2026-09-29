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

from catalog.services import warehouses


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
