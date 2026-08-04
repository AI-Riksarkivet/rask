"""Deletion protection on the TABLE and NAMESPACE rungs (#73, open_lakehouse_diff §1.1).

The warehouse door's Decision-5 contract extended to where the gap was sharpest: a table drop
deletes BYTES, and until the trash-namespace undrop lands nothing stands behind it. The invariants
under test are the contract's whole content:

- a protected object REFUSES its destructive doors 409 (drop, deregister, rename — rename deletes
  the source's bytes);
- ``force=true`` turns the protection lock and NOTHING else;
- the guard runs BEFORE the native call, so a refused drop leaves the object untouched;
- the record dies with the object — a reused id must not inherit protection nobody set on it;
- an unprotected object's doors behave exactly as before (the flag is opt-in).

Registry round-trips against a LOCAL filesystem control root (the real store primitive, no mocks);
the native namespace is a recording stand-in so a refusal can assert the native call NEVER ran.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from catalog.api import fga_deps
from catalog.core.config import Settings
from catalog.core.control_emit import NoopControlEmitter
from lance_namespace import NamespaceNotEmptyError, TableNotFoundError

from service_kit.lakehouse import protection


def _settings(tmp_path: Any, *, grace_days: int = 0) -> Settings:
    """``grace_days=0`` is also the SHIPPED default (#75): recoverable drops are opt-in, because a
    grace period changes what `drop_table` means for every existing caller. The trash tests below
    pass it explicitly, which is exactly how a deployment turns the feature on."""
    data: dict[str, object] = {
        "trash_grace_days": grace_days,
        "control_root": f"file://{tmp_path}",
        "s3_access_key_id": "x",
        "s3_secret_access_key": "x",
        "s3_endpoint_url": "http://localhost:9",
    }
    return Settings.model_validate(data)


class _NoopLineage:
    """The lineage protocol's write half, doing nothing — the doors' happy path awaits it."""

    async def emit_write(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def emit_create(self, *args: Any, **kwargs: Any) -> None:
        return None


class _RecordingNamespace:
    """Counts native calls — a refused door must never reach the backend."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def drop_table(self, request: Any) -> Any:
        self.calls.append("drop_table")
        return type("R", (), {"model_fields_set": set()})()

    def deregister_table(self, request: Any) -> Any:
        self.calls.append("deregister_table")
        return type("R", (), {"model_fields_set": set()})()

    def drop_namespace(self, request: Any) -> Any:
        self.calls.append("drop_namespace")
        return type("R", (), {"model_fields_set": set()})()


def _protect(settings: Settings, kind: str, canonical: str) -> None:
    protection.set_protection(
        settings.registry_root,
        settings.storage_options(),
        {"kind": kind, "id": canonical, "protected": "true", "set_by": "test"},
    )


# ---------------------------------------------------------------- the store


def test_protection_roundtrip_and_absence_means_unprotected(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    so = settings.storage_options()
    assert protection.get_protection(settings.registry_root, so, "table", "bronze$pages") is None
    _protect(settings, "table", "bronze$pages")
    record = protection.get_protection(settings.registry_root, so, "table", "bronze$pages")
    assert record is not None and record["protected"] == "true"
    assert protection.clear_protection(settings.registry_root, so, "table", "bronze$pages") is True
    assert protection.get_protection(settings.registry_root, so, "table", "bronze$pages") is None
    assert protection.clear_protection(settings.registry_root, so, "table", "bronze$pages") is False


def test_kinds_do_not_collide(tmp_path: Any) -> None:
    """A namespace and a table sharing a canonical id are separate records."""
    settings = _settings(tmp_path)
    so = settings.storage_options()
    _protect(settings, "table", "bronze")
    assert protection.get_protection(settings.registry_root, so, "namespace", "bronze") is None


# ---------------------------------------------------------------- the guard contract


def test_guard_refuses_protected_and_force_overrides(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    so = settings.storage_options()
    _protect(settings, "table", "bronze$pages")
    record = protection.get_protection(settings.registry_root, so, "table", "bronze$pages") or {}
    with pytest.raises(NamespaceNotEmptyError, match="protected against deletion"):
        fga_deps.require_not_protected(record, kind="table", obj_id="bronze$pages", force=False)
    fga_deps.require_not_protected(record, kind="table", obj_id="bronze$pages", force=True)  # no raise


# ---------------------------------------------------------------- the doors (shipped handlers)


def _drop_table(settings: Settings, ns: Any, *, force: bool = False) -> Any:
    from catalog.api.v1.endpoints import tables as t_ep

    return asyncio.run(
        t_ep.drop_table(
            id="bronze$pages",
            ns=ns,  # structural stand-in, recorded not mocked
            settings=settings,
            client=None,
            emitter=_NoopLineage(),  # type: ignore[arg-type]
            control=NoopControlEmitter(),
            token=None,
            authorization=None,
            force=force,
        )
    )


def test_a_protected_table_refuses_drop_and_the_native_call_never_ran(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    with pytest.raises(NamespaceNotEmptyError, match="protected against deletion"):
        _drop_table(settings, ns)
    assert ns.calls == [], "the guard must run BEFORE the native call — bytes were touched"


def test_force_drops_a_protected_table_and_the_record_dies_with_it(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    so = settings.storage_options()
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    _drop_table(settings, ns, force=True)
    assert ns.calls == ["drop_table"]
    assert protection.get_protection(settings.registry_root, so, "table", "bronze$pages") is None, "a reused id must not inherit protection nobody set on it"


def test_an_unprotected_table_drops_exactly_as_before(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    ns: Any = _RecordingNamespace()
    _drop_table(settings, ns)
    assert ns.calls == ["drop_table"]


def test_a_protected_table_refuses_deregister_too(tmp_path: Any) -> None:
    """Deregister keeps bytes but removes the object from GOVERNANCE — leaving it ungated would make
    'deregister, then delete the files by hand' the unprotected path around the protected drop."""
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path)
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    with pytest.raises(NamespaceNotEmptyError, match="protected against deletion"):
        asyncio.run(
            t_ep.deregister_table(
                id="bronze$pages",
                ns=ns,
                settings=settings,
                client=None,
                emitter=_NoopLineage(),  # type: ignore[arg-type]
                control=NoopControlEmitter(),
                token=None,
                authorization=None,
            )
        )
    assert ns.calls == []


def test_a_protected_namespace_refuses_drop(tmp_path: Any) -> None:
    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path)
    _protect(settings, "namespace", "bronze")
    ns: Any = _RecordingNamespace()
    with pytest.raises(NamespaceNotEmptyError, match="protected against deletion"):
        asyncio.run(
            n_ep.drop_namespace(
                id="bronze",
                ns=ns,
                settings=settings,
                token=None,
                client=None,
                control=NoopControlEmitter(),
                body=None,
            )
        )
    assert ns.calls == []


# ---------------------------------------------------------------- the protection doors themselves


def test_the_protection_door_sets_and_clears_the_record(tmp_path: Any) -> None:
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path)
    so = settings.storage_options()
    result = asyncio.run(
        t_ep.set_table_protection(
            id="bronze$pages",
            body=t_ep.SetProtectionRequest(protected=True),
            settings=settings,
            token=None,
            control=NoopControlEmitter(),
        )
    )
    assert result.protected is True
    assert (protection.get_protection(settings.registry_root, so, "table", "bronze$pages") or {})["set_by"] == "anonymous"
    result = asyncio.run(
        t_ep.set_table_protection(
            id="bronze$pages",
            body=t_ep.SetProtectionRequest(protected=False),
            settings=settings,
            token=None,
            control=NoopControlEmitter(),
        )
    )
    assert result.protected is False
    assert protection.get_protection(settings.registry_root, so, "table", "bronze$pages") is None


def test_the_protection_suffix_is_owner_gated_not_writer(tmp_path: Any) -> None:
    """The authz map is where a forgotten entry silently falls to WRITER tier — pin both kinds."""
    assert fga_deps._OWNER_SUFFIX_RELATION["table"]["protection"] == "can_drop"  # noqa: SLF001
    assert fga_deps._OWNER_SUFFIX_RELATION["namespace"]["protection"] == "can_delete"  # noqa: SLF001


# ---------------------------------------------------------------- #75 trash / undrop


def test_expired_selects_only_past_deadlines_and_never_deletes(tmp_path: Any) -> None:
    """``expired`` is a SELECTION. It is the report half of a reclaimer whose false positive costs a
    table someone was still inside their window to recover."""
    from datetime import UTC, datetime, timedelta

    from service_kit.lakehouse import trash

    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    fresh = trash.make_record("ns$fresh", location="s3://b/fresh", dropped_by="u", grace_days=7, now=now)
    old = trash.make_record("ns$old", location="s3://b/old", dropped_by="u", grace_days=7, now=now - timedelta(days=30))
    due = trash.expired([fresh, old], now=now)
    assert [r["id"] for r in due] == ["ns$old"]


def test_an_undated_or_unparseable_record_is_NOT_expired(tmp_path: Any) -> None:
    """ "We do not know when this expires" must read as "not yet" — the same fail-toward-not-deleting
    stance the sweep takes when the policy registry is unreadable."""
    from service_kit.lakehouse import trash

    assert trash.expired([{"id": "a"}, {"id": "b", "expires_at": "not-a-date"}]) == []


def test_the_deadline_is_stamped_at_drop_time(tmp_path: Any) -> None:
    """Shortening the estate grace period must never retroactively destroy a live window, so the
    deadline is DATA on the record, not policy consulted at expiry."""
    from datetime import UTC, datetime

    from service_kit.lakehouse import trash

    now = datetime(2026, 8, 4, tzinfo=UTC)
    record = trash.make_record("ns$t", location="s3://b/t", dropped_by="u", grace_days=3, now=now)
    assert record["expires_at"].startswith("2026-08-07")


def test_trash_roundtrip_and_listing(tmp_path: Any) -> None:
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path)
    so = settings.storage_options()
    assert trash.list_all(settings.registry_root, so) == []
    trash.put(settings.registry_root, so, trash.make_record("ns$t", location="s3://b/t", dropped_by="u", grace_days=7))
    assert trash.get(settings.registry_root, so, "ns$t") is not None
    assert len(trash.list_all(settings.registry_root, so)) == 1
    assert trash.clear(settings.registry_root, so, "ns$t") is True
    assert trash.get(settings.registry_root, so, "ns$t") is None


class _TrashableNamespace(_RecordingNamespace):
    """Adds the describe/deregister/register trio the #75 drop→undrop path drives."""

    def describe_table(self, request: Any) -> Any:
        self.calls.append("describe_table")
        return type("R", (), {"location": "s3://bkt/bronze/pages.lance", "model_fields_set": set()})()

    def register_table(self, request: Any) -> Any:
        self.calls.append(f"register_table:{request.location}")
        return type("R", (), {"location": request.location, "model_fields_set": set()})()


def test_a_drop_with_a_grace_period_DEREGISTERS_and_files_trash(tmp_path: Any) -> None:
    """#75: the drop that used to destroy bytes now detaches them and records the deadline — the whole
    point, because time-travel cannot recover a drop (restore_table rewinds a LIVE table)."""
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    _drop_table(settings, ns)
    assert "drop_table" not in ns.calls, "the bytes were destroyed despite a grace period"
    assert "deregister_table" in ns.calls
    record = trash.get(settings.registry_root, settings.storage_options(), "bronze$pages")
    assert record is not None and record["location"] == "s3://bkt/bronze/pages.lance"


def test_purge_true_still_destroys_immediately(tmp_path: Any) -> None:
    """The explicit opt-out: a caller who means 'destroy the bytes now' says so, and no trash is filed."""
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    asyncio.run(
        __import__("catalog.api.v1.endpoints.tables", fromlist=["tables"]).drop_table(
            id="bronze$pages",
            ns=ns,
            settings=settings,
            client=None,
            emitter=_NoopLineage(),
            control=NoopControlEmitter(),
            token=None,
            authorization=None,
            force=False,
            purge=True,
        )
    )
    assert "drop_table" in ns.calls
    assert trash.get(settings.registry_root, settings.storage_options(), "bronze$pages") is None


def test_undrop_re_registers_from_the_trash_record_and_clears_it(tmp_path: Any) -> None:
    from catalog.api.v1.endpoints import tables as t_ep

    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    _drop_table(settings, ns)
    asyncio.run(t_ep.undrop_table(id="bronze$pages", ns=ns, settings=settings, token=None, client=None, control=NoopControlEmitter()))
    assert "register_table:s3://bkt/bronze/pages.lance" in ns.calls
    assert trash.get(settings.registry_root, settings.storage_options(), "bronze$pages") is None


def test_undrop_without_a_record_is_an_honest_404(tmp_path: Any) -> None:
    """An expired or never-trashed drop is genuinely unrecoverable — say so, rather than a 200 that
    recovers nothing."""
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    with pytest.raises(TableNotFoundError, match="no recoverable drop"):
        asyncio.run(t_ep.undrop_table(id="bronze$gone", ns=ns, settings=settings, token=None, client=None, control=NoopControlEmitter()))


def test_the_tasks_door_shows_the_pending_expiry(tmp_path: Any) -> None:
    """§2.4 per-object task visibility: an undrop deadline the owner cannot see is not a safety feature."""
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    assert asyncio.run(t_ep.table_tasks(id="bronze$pages", settings=settings, token=None)) == []
    _drop_table(settings, ns)
    tasks = asyncio.run(t_ep.table_tasks(id="bronze$pages", settings=settings, token=None))
    assert len(tasks) == 1 and tasks[0].location == "s3://bkt/bronze/pages.lance" and tasks[0].expires_at
