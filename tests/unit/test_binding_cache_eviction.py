"""Cross-replica binding-cache invalidation rides the control-event broadcast (#46).

The cache's premise — bindings are immutable, cache positives forever — was broken by the warehouse
delete, and the eviction that existed was the deleting replica popping its OWN dict: with
`values-prod.yaml`'s `services.catalog.replicas: 2` the second replica kept routing a dropped
namespace at the deleted warehouse's bucket. The fix keys eviction off the events every replica
already receives (the broadcast subscription has no queueGroup), so these tests drive the SAME
handler the Dapr sidecar POSTs to, with the same event shapes the doors publish.
"""

from __future__ import annotations

import asyncio
from typing import Any

from catalog.api.dapr import on_control_event
from catalog.services.warehouses import evict_stale_bindings


def _cache() -> dict[str, dict[str, str]]:
    return {
        "acme": {"warehouse_id": "wh-acme", "root_uri": "s3://acme-bucket"},
        "acme_gold": {"warehouse_id": "wh-acme", "root_uri": "s3://acme-bucket"},
        "beta": {"warehouse_id": "wh-beta", "root_uri": "s3://beta-bucket"},
    }


# ---------------------------------------------------------------- the eviction rules


def _settings() -> Any:
    """The settings the handler now takes by INJECTION rather than calling `get_settings()` in its body.

    That change is the point of open_fastapi-audit's DI-seam finding: a body call opts the route out of
    `app.dependency_overrides`. A direct call like this one has to pass what FastAPI would have
    resolved, so the test exercises the same value the route receives in production.
    """
    from catalog.core.config import Settings

    return Settings.model_validate({"LANCE_REST_IMPL": "dir", "LANCE_S3_ACCESS_KEY_ID": "k", "LANCE_S3_SECRET_ACCESS_KEY": "s"})


def test_warehouse_deleted_evicts_every_binding_to_that_warehouse() -> None:
    """Both eviction sources fire: the event's own namespaces_dropped list AND the warehouse-id scan
    (a Decision-3 partial delete can unbind more than the event recorded)."""
    cache = _cache()
    evicted = evict_stale_bindings(
        cache,
        action="warehouse_deleted",
        object_id="warehouse:wh-acme",
        extra={"namespaces_dropped": ["acme"]},  # the scan must still catch acme_gold
        delimiter="$",
    )
    assert sorted(evicted) == ["acme", "acme_gold"]
    assert set(cache) == {"beta"}, "an unrelated tenant's binding was evicted"


def test_namespace_dropped_evicts_the_TOP_segment() -> None:
    """The cache is keyed by top-level namespace, so the id's FIRST segment is what gets evicted —
    for a top-level drop because its binding is gone, for a nested drop as a harmless re-read (the
    binding is intact; the next resolve re-caches it). Both must map to `acme`, never `acme$inner`
    (a whole-id pop would MISS the top-level key and leave replica 2 routing at a dead bucket)."""
    cache = _cache()
    assert evict_stale_bindings(cache, action="namespace_dropped", object_id="namespace:acme", extra={}, delimiter="$") == ["acme"]
    assert "acme" not in cache and "beta" in cache
    fresh = _cache()
    assert evict_stale_bindings(fresh, action="namespace_dropped", object_id="namespace:acme$inner", extra={}, delimiter="$") == ["acme"]
    assert "beta" in fresh


def test_warehouse_bound_evicts_the_rebound_namespace() -> None:
    """A cached entry for a namespace being BOUND can only be a pre-delete leftover this replica
    never heard the delete for — the next request must read the authoritative record."""
    cache = _cache()
    evicted = evict_stale_bindings(cache, action="warehouse_bound", object_id="warehouse:wh-new", extra={"namespace": "acme"}, delimiter="$")
    assert evicted == ["acme"]
    assert set(cache) == {"acme_gold", "beta"}


def test_unrelated_events_evict_nothing() -> None:
    cache = _cache()
    for action, obj in [("table_created", "table:acme$t"), ("grant_added", "table:acme$t"), ("warehouse_deactivated", "warehouse:wh-acme")]:
        assert evict_stale_bindings(cache, action=action, object_id=obj, extra={}, delimiter="$") == []
    assert len(cache) == 3, "a non-binding event evicted a binding"
    # Deactivation is deliberately absent from the rules: warehouse STATUS is read live per request.


# ---------------------------------------------------------------- through the real handler


class _State:
    def __init__(self, cache: dict[str, dict[str, str]]) -> None:
        self.warehouse_binding_cache = cache
        self.control_buffer = None


class _App:
    def __init__(self, cache: dict[str, dict[str, str]]) -> None:
        self.state = _State(cache)


class _Request:
    def __init__(self, cache: dict[str, dict[str, str]]) -> None:
        self.app = _App(cache)


def test_the_dapr_handler_evicts_on_a_delivered_event(monkeypatch: Any) -> None:
    """End-to-end through `on_control_event` — the exact route the sidecar POSTs, with the exact
    CloudEvent envelope shape (`body["data"]` carries the CatalogControlEvent)."""
    from catalog.core.config import get_settings

    # The handler reads the delimiter off the process settings; stub the required creds the way the
    # live catalog boot does and drop the lru_cache so THIS env is what gets read. Cleared again in
    # the finally so the stub-built Settings cannot leak into a later test's get_settings().
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "x")
    get_settings.cache_clear()
    try:
        cache = _cache()
        body = {
            "data": {
                "action": "warehouse_deleted",
                "object_type": "warehouse",
                "object_id": "warehouse:wh-acme",
                "extra": {"namespaces_dropped": ["acme"], "project": "acme", "bucket": "acme-bucket"},
            }
        }
        request: Any = _Request(cache)  # structural stand-in, same pattern as the door tests
        result = asyncio.run(on_control_event(body, request, _settings(), None))
        assert result == {"status": "SUCCESS"}
        assert set(cache) == {"beta"}, "the handler did not evict — replica 2 still routes at the deleted bucket"
    finally:
        get_settings.cache_clear()


def test_a_malformed_event_is_dropped_and_evicts_nothing() -> None:
    cache = _cache()
    request: Any = _Request(cache)
    result = asyncio.run(on_control_event({"data": {"action": "not-a-real-action"}}, request, _settings(), None))
    assert result == {"status": "SUCCESS"}
    assert len(cache) == 3


def test_a_handler_without_the_cache_still_acks() -> None:
    """A deployment shape without the cache on state (defensive getattr) must not 500 the sidecar."""

    class _BareApp:
        class state:  # noqa: N801 — structural stand-in
            control_buffer = None

    class _BareRequest:
        app = _BareApp()

    body = {"data": {"action": "warehouse_deleted", "object_type": "warehouse", "object_id": "warehouse:w", "extra": {}}}
    request: Any = _BareRequest()
    assert asyncio.run(on_control_event(body, request, _settings(), None)) == {"status": "SUCCESS"}
