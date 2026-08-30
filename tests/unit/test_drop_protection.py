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
from lance_namespace import NamespaceNotEmptyError, TableNotFoundError

from catalog.api import fga_deps
from catalog.core.config import Settings
from service_kit.control_emit import NoopControlEmitter
from service_kit.lakehouse import protection


def _request_stub() -> Any:
    """The minimum `undrop_namespace` touches on a Request: the per-root connection cache.

    It re-derives its namespace connection from the trash record's binding (diff2 F6 leg c), because
    `NamespaceDep` resolves BEFORE the handler body and — the binding having been removed by the drop
    — has already fallen back to the estate's default root. These tests drive the handler directly
    rather than over HTTP, so they supply the one attribute that path reads.
    """
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(warehouse_namespaces={})))


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

    async def project_for(self, top_ns: str) -> str | None:
        """Part of `LineageEmitter` since the tenant became `lance.project` (WATCH targeting's key).
        `None` is the honest answer for a fake with no registry behind it."""
        return None

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

    def list_namespaces(self, request: Any) -> Any:
        return type("R", (), {"namespaces": [], "page_token": None})()

    def list_tables(self, request: Any) -> Any:
        tables = ["pages"] if request.id == ["bronze"] else []
        return type("R", (), {"tables": tables, "page_token": None})()


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
    asyncio.run(t_ep.undrop_table(id="bronze$pages", ns=ns, settings=settings, token=None, control=NoopControlEmitter()))
    assert "register_table:pages.lance" in ns.calls, "the RELATIVE form register_table accepts"
    assert trash.get(settings.registry_root, settings.storage_options(), "bronze$pages") is None


def test_undrop_without_a_record_is_an_honest_404(tmp_path: Any) -> None:
    """An expired or never-trashed drop is genuinely unrecoverable — say so, rather than a 200 that
    recovers nothing."""
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    with pytest.raises(TableNotFoundError, match="no recoverable drop"):
        asyncio.run(t_ep.undrop_table(id="bronze$gone", ns=ns, settings=settings, token=None, control=NoopControlEmitter()))


def test_the_tasks_door_shows_the_pending_expiry(tmp_path: Any) -> None:
    """§2.4 per-object task visibility: an undrop deadline the owner cannot see is not a safety feature."""
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    assert asyncio.run(t_ep.table_tasks(id="bronze$pages", settings=settings)) == []
    _drop_table(settings, ns)
    tasks = asyncio.run(t_ep.table_tasks(id="bronze$pages", settings=settings))
    assert len(tasks) == 1 and tasks[0].location == "s3://bkt/bronze/pages.lance" and tasks[0].expires_at


def test_a_recoverable_drop_KEEPS_the_owner_grants(tmp_path: Any) -> None:
    """Found by driving the DEPLOYED catalog, not by a unit test (these run FGA off): the drop revoked
    the table's tuples unconditionally, so after a recoverable drop the one person who needed to undrop
    it — its owner — was denied. A recoverable drop leaves an object that still exists; its grants die
    with the BYTES (at purge, or when expiry reclaims it), not with the pointer."""
    from unittest.mock import AsyncMock

    from catalog.api import fga_deps as deps

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    revoked = AsyncMock()
    original = deps.revoke_ownership
    deps.revoke_ownership = revoked  # type: ignore[assignment]
    try:
        _drop_table(settings, ns)
    finally:
        deps.revoke_ownership = original  # type: ignore[assignment]
    revoked.assert_not_awaited()


def test_a_DESTRUCTIVE_drop_still_revokes(tmp_path: Any) -> None:
    """The negative twin — the reuse rule still holds where the bytes really are gone."""
    from unittest.mock import AsyncMock

    from catalog.api import fga_deps as deps

    settings = _settings(tmp_path, grace_days=0)
    ns: Any = _TrashableNamespace()
    revoked = AsyncMock()
    original = deps.revoke_ownership
    deps.revoke_ownership = revoked  # type: ignore[assignment]
    try:
        _drop_table(settings, ns)
    finally:
        deps.revoke_ownership = original  # type: ignore[assignment]
    revoked.assert_awaited_once()


def test_the_tasks_suffix_is_reader_tier_not_writer(tmp_path: Any) -> None:
    """The live audit's other finding: `tasks` was unmapped, so it fell through to WRITER — and after a
    drop the owner has no write rung, making their own deadline unreadable."""
    assert "tasks" in fga_deps._META_READ_ACTIONS  # noqa: SLF001


def test_undrop_registers_a_RELATIVE_location(tmp_path: Any) -> None:
    """`register_table` refuses absolute URIs ("Location must be a relative path within the root
    directory") — undrop 400'd live on the very location describe_table had just reported. The `dir`
    backend lays table dirs out FLAT under the connection root, so the relative form is the final
    segment; the record keeps the absolute URI because that is what an operator reading trash needs."""
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    _drop_table(settings, ns)
    asyncio.run(t_ep.undrop_table(id="bronze$pages", ns=ns, settings=settings, token=None, control=NoopControlEmitter()))
    registered = [c for c in ns.calls if c.startswith("register_table:")]
    assert registered == ["register_table:pages.lance"], registered
    assert "://" not in registered[0], "an absolute URI reached register_table — the live 400"


def test_a_CASCADE_refuses_when_a_descendant_is_protected(tmp_path: Any) -> None:
    """A cascade destroys children INSIDE one native call — they never reach a door. Before this, a
    protected table under a cascade-dropped namespace died silently while the docstring claimed
    otherwise. The refusal must NAME the protected descendant: "something in here is protected" is
    not an answer anyone can act on."""
    from lance_namespace import DropNamespaceRequest

    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path)
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    with pytest.raises(NamespaceNotEmptyError, match="bronze\\$pages"):
        asyncio.run(
            n_ep.drop_namespace(
                id="bronze",
                ns=ns,
                settings=settings,
                token=None,
                client=None,
                control=NoopControlEmitter(),
                body=DropNamespaceRequest(behavior="Cascade"),
            )
        )
    assert ns.calls == [], "the cascade ran despite a protected descendant"


def test_force_lets_a_cascade_through(tmp_path: Any) -> None:
    """The negative twin — force turns the protection lock on the subtree exactly as at the named rung."""
    from lance_namespace import DropNamespaceRequest

    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path)
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    asyncio.run(
        n_ep.drop_namespace(
            id="bronze",
            ns=ns,
            settings=settings,
            token=None,
            client=None,
            control=NoopControlEmitter(),
            body=DropNamespaceRequest(behavior="Cascade"),
            force=True,
        )
    )
    assert "drop_namespace" in ns.calls


# ---------------------------------------------------------------- #96 the recoverable cascade


class _CascadableNamespace(_TrashableNamespace):
    """A two-level subtree — bronze → [pages, inner → [t2]] — enough shape to prove ordering.

    Every mutating call is recorded WITH its id (and drop_namespace with its behavior), because the
    defect under test is precisely WHICH native calls a cascade makes: the destructive one destroys
    children inside one `drop_namespace(cascade)`, the recoverable one must never issue it.
    """

    def list_tables(self, request: Any) -> Any:
        tables = {("bronze",): ["pages"], ("bronze", "inner"): ["t2"]}.get(tuple(request.id), [])
        return type("R", (), {"tables": tables, "page_token": None})()

    def list_namespaces(self, request: Any) -> Any:
        children = ["inner"] if request.id == ["bronze"] else []
        return type("R", (), {"namespaces": children, "page_token": None})()

    def describe_table(self, request: Any) -> Any:
        self.calls.append(f"describe_table:{'$'.join(request.id)}")
        return type("R", (), {"location": f"s3://bkt/{'_'.join(request.id)}.lance", "model_fields_set": set()})()

    def deregister_table(self, request: Any) -> Any:
        self.calls.append(f"deregister_table:{'$'.join(request.id)}")
        return type("R", (), {"model_fields_set": set()})()

    def drop_namespace(self, request: Any) -> Any:
        self.calls.append(f"drop_namespace:{'$'.join(request.id)}:{(request.behavior or 'restrict').lower()}")
        return type("R", (), {"model_fields_set": set()})()

    def create_namespace(self, request: Any) -> Any:
        self.calls.append(f"create_namespace:{'$'.join(request.id)}:{(request.mode or 'create').lower()}")
        return type("R", (), {"model_fields_set": set()})()


def _drop_namespace_cascade(settings: Settings, ns: Any, *, force: bool = False, purge: bool = False) -> Any:
    from lance_namespace import DropNamespaceRequest

    from catalog.api.v1.endpoints import namespaces as n_ep

    return asyncio.run(
        n_ep.drop_namespace(
            id="bronze",
            ns=ns,
            settings=settings,
            token=None,
            client=None,
            control=NoopControlEmitter(),
            body=DropNamespaceRequest(behavior="Cascade"),
            force=force,
            purge=purge,
        )
    )


def test_a_recoverable_cascade_DETACHES_the_subtree_and_files_a_record_per_child(tmp_path: Any) -> None:
    """#96's whole content. A trash record pointing at bytes the native cascade deleted would be a
    lie, so with a grace period the cascade must never issue the destructive native call: tables are
    DEREGISTERED (bytes stay), namespaces emptied then dropped plainly, and every destroyed object —
    both tables, both namespaces — has a trash record undrop can act on."""
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _CascadableNamespace()
    _drop_namespace_cascade(settings, ns)

    assert not any(c.endswith(":cascade") for c in ns.calls), "the destructive native cascade ran despite a grace period"
    assert "deregister_table:bronze$pages" in ns.calls
    assert "deregister_table:bronze$inner$t2" in ns.calls
    # Namespaces fall deepest-first, and only after every table is detached — each drop hits an
    # already-empty namespace (restrict semantics hold without relying on them).
    drops = [c for c in ns.calls if c.startswith("drop_namespace:")]
    assert drops == ["drop_namespace:bronze$inner:restrict", "drop_namespace:bronze:restrict"]
    assert ns.calls.index("deregister_table:bronze$inner$t2") < ns.calls.index(drops[0])

    so = settings.storage_options()
    pages = trash.get(settings.registry_root, so, "bronze$pages")
    assert pages is not None and pages["location"] == "s3://bkt/bronze_pages.lance"
    assert trash.get(settings.registry_root, so, "bronze$inner$t2") is not None
    assert trash.get(settings.registry_root, so, "bronze", kind="namespace") is not None
    assert trash.get(settings.registry_root, so, "bronze$inner", kind="namespace") is not None


def test_cascade_purge_true_still_destroys_natively_and_files_nothing(tmp_path: Any) -> None:
    """The explicit opt-out, same word as the table door: purge means destroy the bytes now.

    It destroys BOTTOM-UP rather than forwarding `behavior=Cascade`: the `dir` backend the chart runs
    does not implement the native cascade at all (#117), so this assertion used to pin a call that
    answered NamespaceNotEmpty in production while the test passed against a fake that honoured it.
    """
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _CascadableNamespace()
    _drop_namespace_cascade(settings, ns, purge=True)
    assert "drop_namespace:bronze:restrict" in ns.calls  # the root, once every child is gone
    assert not any(c.endswith(":cascade") for c in ns.calls)
    assert not any(c.startswith("deregister_table:") for c in ns.calls)
    assert trash.list_all(settings.registry_root, settings.storage_options()) == []


def test_cascade_with_grace_zero_is_the_shipped_default_and_unchanged(tmp_path: Any) -> None:
    """Recoverable drops are opt-in (#75): without a grace period the cascade destroys, no records.

    "Exactly what it always was" was the wrong bar — what it always was is a native cascade the
    shipped `dir` backend refuses (#117). The destruction is now ours, bottom-up.
    """
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=0)
    ns: Any = _CascadableNamespace()
    _drop_namespace_cascade(settings, ns)
    assert "drop_namespace:bronze:restrict" in ns.calls
    assert not any(c.endswith(":cascade") for c in ns.calls)
    assert trash.list_all(settings.registry_root, settings.storage_options()) == []


def test_a_recoverable_cascade_KEEPS_the_fga_tuples(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The #75 rule at the subtree scale: the owner is the one person who needs to undrop it, so the
    grants die with the bytes (purge/expiry), never with a recoverable drop."""
    revoked: list[str] = []

    async def _recording_revoke(client: Any, settings: Any, *, resource: str, segments: list[str], token: Any) -> None:
        revoked.append(f"{resource}:{'$'.join(segments)}")

    monkeypatch.setattr(fga_deps, "revoke_ownership", _recording_revoke)

    settings = _settings(tmp_path, grace_days=7)
    _drop_namespace_cascade(settings, _CascadableNamespace())
    assert revoked == [], "a recoverable cascade revoked tuples its own undrop needs"

    _drop_namespace_cascade(settings, _CascadableNamespace(), purge=True)
    assert "namespace:bronze" in revoked and "table:bronze$inner$t2" in revoked, "the destructive path must still revoke"


def test_namespace_undrop_rebuilds_the_whole_subtree(tmp_path: Any) -> None:
    """The plural undrop: namespaces shallowest-first, then every table re-registered at its old id
    from the RELATIVE location form (#75's dir-backend lesson), and the records cleared."""
    from catalog.api.v1.endpoints import namespaces as n_ep
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _CascadableNamespace()
    _drop_namespace_cascade(settings, ns)
    ns.calls.clear()

    asyncio.run(n_ep.undrop_namespace(id="bronze", request=_request_stub(), ns=ns, settings=settings, token=None, control=NoopControlEmitter()))

    creates = [c for c in ns.calls if c.startswith("create_namespace:")]
    assert creates == ["create_namespace:bronze:exist_ok", "create_namespace:bronze$inner:exist_ok"], "parents must exist before children"
    assert "register_table:bronze_pages.lance" in ns.calls
    assert "register_table:bronze_inner_t2.lance" in ns.calls
    assert ns.calls.index(creates[1]) < ns.calls.index("register_table:bronze_inner_t2.lance")
    assert trash.list_all(settings.registry_root, settings.storage_options()) == [], "a recovered record must be cleared"


def test_namespace_undrop_without_a_record_is_an_honest_404(tmp_path: Any) -> None:
    from lance_namespace import NamespaceNotFoundError

    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _CascadableNamespace()  # structural stand-in, same as every door test here
    with pytest.raises(NamespaceNotFoundError, match="no recoverable drop"):
        asyncio.run(n_ep.undrop_namespace(id="bronze", request=_request_stub(), ns=ns, settings=settings, token=None, control=NoopControlEmitter()))


def test_namespace_tasks_reports_the_pending_expiry(tmp_path: Any) -> None:
    """The deadline the owner is racing must be visible on the namespace rung too."""
    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path, grace_days=7)
    assert asyncio.run(n_ep.namespace_tasks(id="bronze", settings=settings)) == []
    _drop_namespace_cascade(settings, _CascadableNamespace())
    entries = asyncio.run(n_ep.namespace_tasks(id="bronze", settings=settings))
    assert len(entries) == 1 and entries[0].expires_at, "the undrop deadline is invisible"


def test_trash_kinds_do_not_collide(tmp_path: Any) -> None:
    """A namespace and a table sharing a canonical id are separate trash records — same rule the
    protection store already pins."""
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path)
    so = settings.storage_options()
    trash.put(settings.registry_root, so, trash.make_record("bronze", location="s3://b/t", dropped_by="u", grace_days=7))
    assert trash.get(settings.registry_root, so, "bronze", kind="namespace") is None
    assert trash.get(settings.registry_root, so, "bronze") is not None


def test_the_undrop_suffixes_are_owner_gated_not_writer(tmp_path: Any) -> None:
    """The authz map is where a forgotten entry silently falls to WRITER tier — pin both kinds (#96
    extends #75's table rule to the namespace rung)."""
    assert fga_deps._OWNER_SUFFIX_RELATION["table"]["undrop"] == "can_drop"  # noqa: SLF001
    assert fga_deps._OWNER_SUFFIX_RELATION["namespace"]["undrop"] == "can_delete"  # noqa: SLF001


def test_a_destructive_drop_of_a_top_level_namespace_removes_its_WAREHOUSE_BINDING(tmp_path: Any) -> None:
    """`unbind_namespace` existed and only the warehouse-delete door called it, so a direct drop of a
    top-level namespace leaked its binding — and the UI derives its namespace list from bindings, so
    the dropped namespace kept LISTING forever. Seen on the deployed estate: `casc9` survived its own
    successful cascade as a phantom row."""
    from catalog.services import warehouses

    settings = _settings(tmp_path, grace_days=0)
    so = settings.storage_options()
    warehouses.bind_namespace(settings.registry_root, so, "bronze", "wh1", "s3://wh1-bucket")
    assert warehouses.binding_for_namespace(settings.registry_root, so, "bronze") is not None

    _drop_namespace_cascade(settings, _CascadableNamespace())
    assert warehouses.binding_for_namespace(settings.registry_root, so, "bronze") is None, "the binding outlived the namespace"


def test_a_RECOVERABLE_cascade_UNBINDS_and_records_the_binding_for_the_undrop(tmp_path: Any) -> None:
    """REVERSED by owner ruling (diff2 F6 leg c). This asserted the opposite until 2026-08-15.

    The binding used to be KEPT on a recoverable drop, on the same reasoning as the grants (#75):
    undrop rebuilds the subtree and needs a route. The cost was that it OUTLIVED the namespace — and
    when the grace window expired, the purge reclaimed the bytes and left the binding standing
    forever, invisible to every reconciler category (`dangling_bindings` keys on a MISSING warehouse
    record, and the warehouse is still there).

    So the drop unbinds, and the ROOT trash record carries `{warehouse_id, root_uri}` as the only
    surviving copy — which undrop re-binds from, under a deactivation check. The route is not lost;
    it moved onto the record that already had to survive for the undrop to work at all.
    """
    from catalog.services import warehouses
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    so = settings.storage_options()
    warehouses.bind_namespace(settings.registry_root, so, "bronze", "wh1", "s3://wh1-bucket")

    _drop_namespace_cascade(settings, _CascadableNamespace())

    assert warehouses.binding_for_namespace(settings.registry_root, so, "bronze") is None, "the binding outlived its namespace"
    record = trash.get(settings.registry_root, so, "bronze", kind="namespace")
    assert record is not None
    assert record.get("binding") == {"warehouse_id": "wh1", "root_uri": "s3://wh1-bucket"}, "the route was lost, not moved"


def test_the_protection_flag_is_READABLE_on_both_rungs(tmp_path: Any) -> None:
    """#123. The SET door shipped with no read at all — an operator could not arm-check, disarm-check,
    or audit protection; they discovered it by eating a 409. The GET returns the flag AND who armed it."""
    from catalog.api.v1.endpoints import namespaces as n_ep
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path)
    so = settings.storage_options()
    protection.set_protection(settings.registry_root, so, {"kind": "table", "id": "db1$t", "protected": "true", "set_by": "user:alice"})

    read = asyncio.run(t_ep.get_table_protection("db1$t", settings=settings))
    assert (read.protected, read.set_by) == (True, "user:alice")
    unarmed = asyncio.run(t_ep.get_table_protection("db1$other", settings=settings))
    assert (unarmed.protected, unarmed.set_by) == (False, None)

    protection.set_protection(settings.registry_root, so, {"kind": "namespace", "id": "bronze", "protected": "true", "set_by": "user:bob"})
    ns_read = asyncio.run(n_ep.get_namespace_protection("bronze", settings=settings))
    assert (ns_read.protected, ns_read.set_by) == (True, "user:bob")
