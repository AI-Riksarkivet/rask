"""The shared warehouse-registry READ half (#84) — project → active warehouse root, on a local-fs
control root (the same pattern as the maintenance-policy registry tests: real records, no S3).

This resolver is what routes a per-tenant medallion trigger into its project's bucket, so the pinned
contracts are the fail-closed ones: unknown project → ``None`` (never a default root), deactivated
warehouses invisible, unsafe project ids rejected at the boundary, and the TTL cache never hides a
freshly provisioned warehouse (misses are not cached).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from service_kit.lakehouse import warehouse_registry
from service_kit.lakehouse.warehouse_registry import clear_cache, is_safe_project, project_gold_root, project_root


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


def _write_record(control_root: Path, record: dict[str, Any]) -> None:
    registry = control_root / "_warehouses"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{record['id']}.json").write_text(json.dumps(record))


def _record(wh_id: str, project: str, root_uri: str, **extra: Any) -> dict[str, Any]:
    return {"id": wh_id, "bucket": f"{project}-bucket", "project": project, "root_uri": root_uri, **extra}


def test_resolves_the_active_warehouse_root(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1/", status="active"))
    assert project_root(str(tmp_path), {}, "acme") == "s3://acme-wh1"  # trailing slash stripped


def test_absent_status_counts_as_active(tmp_path: Path) -> None:
    # Records written before the lifecycle feature carry no status — they are live (catalog parity).
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1"))
    assert project_root(str(tmp_path), {}, "acme") == "s3://acme-wh1"


def test_unknown_project_resolves_to_none(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1", status="active"))
    assert project_root(str(tmp_path), {}, "globex") is None


def test_absent_registry_prefix_resolves_to_none(tmp_path: Path) -> None:
    assert project_root(str(tmp_path), {}, "acme") is None  # no _warehouses/ at all — no crash


def test_deactivated_warehouse_is_invisible(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1", status="deactivated"))
    assert project_root(str(tmp_path), {}, "acme") is None


def test_multiple_active_warehouses_resolve_deterministically(tmp_path: Path) -> None:
    # Routing must never flap between roots: the lowest warehouse id wins, every call.
    _write_record(tmp_path, _record("wh2", "acme", "s3://acme-wh2", status="active"))
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1", status="active"))
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=0) == "s3://acme-wh1"
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=0) == "s3://acme-wh1"


def test_corrupt_record_is_skipped_not_fatal(tmp_path: Path) -> None:
    registry = tmp_path / "_warehouses"
    registry.mkdir(parents=True)
    (registry / "junk.json").write_text("{not json")
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1", status="active"))
    assert project_root(str(tmp_path), {}, "acme") == "s3://acme-wh1"


def test_bindings_subdirectory_is_ignored(tmp_path: Path) -> None:
    bindings = tmp_path / "_warehouses" / "bindings"
    bindings.mkdir(parents=True)
    (bindings / "acme.json").write_text(json.dumps({"top_ns": "acme", "root_uri": "s3://WRONG"}))
    assert project_root(str(tmp_path), {}, "acme") is None  # a binding is not a warehouse record


def test_ttl_cache_serves_hits_and_clear_cache_invalidates(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("wh1", "acme", "s3://old-root", status="active"))
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=3600) == "s3://old-root"
    _write_record(tmp_path, _record("wh1", "acme", "s3://new-root", status="active"))
    # Within the TTL the cached root is served (records are immutable-except-status by contract)...
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=3600) == "s3://old-root"
    # ...and an explicit invalidation (or TTL expiry) re-reads the registry.
    warehouse_registry.clear_cache()
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=3600) == "s3://new-root"


def test_a_miss_is_not_cached_so_fresh_provisioning_resolves_immediately(tmp_path: Path) -> None:
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=3600) is None
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1", status="active"))
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=3600) == "s3://acme-wh1"


def test_default_ttl_is_short_and_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cache only ever serves stale POSITIVES, so the default window a deactivated warehouse can
    # keep resolving through must stay short (≤5s); operators tune it via the env var.
    monkeypatch.delenv("WAREHOUSE_REGISTRY_TTL_SECONDS", raising=False)
    assert warehouse_registry._default_ttl_seconds() <= 5.0
    monkeypatch.setenv("WAREHOUSE_REGISTRY_TTL_SECONDS", "0.5")
    assert warehouse_registry._default_ttl_seconds() == 0.5


def test_invalid_env_ttl_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAREHOUSE_REGISTRY_TTL_SECONDS", "junk")
    assert warehouse_registry._default_ttl_seconds() == warehouse_registry._DEFAULT_TTL_SECONDS


def test_env_ttl_zero_makes_deactivation_immediate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # TTL 0 via the env (no caller changes needed): every call re-reads the registry, so a
    # deactivation is honored on the very next resolution.
    monkeypatch.setenv("WAREHOUSE_REGISTRY_TTL_SECONDS", "0")
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1", status="active"))
    assert project_root(str(tmp_path), {}, "acme") == "s3://acme-wh1"
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-wh1", status="deactivated"))
    assert project_root(str(tmp_path), {}, "acme") is None


def test_ttl_zero_always_rereads(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("wh1", "acme", "s3://old-root", status="active"))
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=0) == "s3://old-root"
    _write_record(tmp_path, _record("wh1", "acme", "s3://new-root", status="active"))
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=0) == "s3://new-root"


@pytest.mark.parametrize("value", ["acme", "a", "Acme-2", "t_1", "x" * 64])
def test_safe_project_ids_pass(value: str) -> None:
    assert is_safe_project(value)


@pytest.mark.parametrize("value", ["", "-acme", "a/b", "a$b", "..", "a b", "a.b", "x" * 65, None, 7, ["acme"]])
def test_unsafe_project_ids_are_rejected(value: object) -> None:
    # These become S3 key prefixes and lineage names — anything path-shaped must be refused, not repaired.
    assert not is_safe_project(value)


# ── gold serving warehouses (DECISIONS "Medallion tiers — hybrid physical layout") ───────────────────


def test_gold_root_resolves_only_serving_gold_records(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-work", status="active"))
    _write_record(tmp_path, _record("wh2", "acme", "s3://acme-gold", status="active", serving="gold"))
    assert project_gold_root(str(tmp_path), {}, "acme") == "s3://acme-gold"
    assert project_root(str(tmp_path), {}, "acme") == "s3://acme-work"


def test_work_root_never_hijacked_by_a_gold_record(tmp_path: Path) -> None:
    # The gold record's id sorts BELOW the work warehouse's — under the old any-record lowest-id rule it
    # would have won project_root and routed raw/bronze/silver into the serving bucket. Serving records
    # are excluded from the work class entirely.
    _write_record(tmp_path, _record("aaa-gold", "acme", "s3://acme-gold", status="active", serving="gold"))
    _write_record(tmp_path, _record("zzz-work", "acme", "s3://acme-work", status="active"))
    assert project_root(str(tmp_path), {}, "acme") == "s3://acme-work"


def test_gold_root_none_when_project_has_no_serving_warehouse(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-work", status="active"))
    assert project_gold_root(str(tmp_path), {}, "acme") is None  # caller falls back to the work root


def test_multiple_gold_warehouses_resolve_deterministically(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("g2", "acme", "s3://acme-gold-2", status="active", serving="gold"))
    _write_record(tmp_path, _record("g1", "acme", "s3://acme-gold-1", status="active", serving="gold"))
    assert project_gold_root(str(tmp_path), {}, "acme", ttl_seconds=0) == "s3://acme-gold-1"


def test_deactivated_gold_warehouse_is_invisible(tmp_path: Path) -> None:
    _write_record(tmp_path, _record("g1", "acme", "s3://acme-gold", status="deactivated", serving="gold"))
    assert project_gold_root(str(tmp_path), {}, "acme") is None


def test_unknown_serving_class_matches_neither_resolver(tmp_path: Path) -> None:
    # Fail closed: a record from a future build (serving="platinum") must not be routed by THIS build as
    # either class — never route a tenant into a warehouse class the resolver does not know.
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-x", status="active", serving="platinum"))
    assert project_root(str(tmp_path), {}, "acme") is None
    assert project_gold_root(str(tmp_path), {}, "acme") is None


def test_gold_and_work_caches_are_independent(tmp_path: Path) -> None:
    # The positive cache is partitioned by serving class — a cached work root must never answer a gold
    # lookup (they are different buckets by design).
    _write_record(tmp_path, _record("wh1", "acme", "s3://acme-work", status="active"))
    assert project_root(str(tmp_path), {}, "acme", ttl_seconds=3600) == "s3://acme-work"
    assert project_gold_root(str(tmp_path), {}, "acme", ttl_seconds=3600) is None
    _write_record(tmp_path, _record("g1", "acme", "s3://acme-gold", status="active", serving="gold"))
    # The gold miss was not cached, so the freshly provisioned serving warehouse resolves immediately.
    assert project_gold_root(str(tmp_path), {}, "acme", ttl_seconds=3600) == "s3://acme-gold"
