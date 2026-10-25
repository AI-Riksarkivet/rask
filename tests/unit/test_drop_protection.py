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
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from lance_namespace import DeregisterTableResponse, InvalidTableStateError, NamespaceNotEmptyError, TableNotFoundError

from catalog.api import fga_deps
from catalog.api.dependencies import Resolver
from catalog.api.idempotency import KeyedDoor
from catalog.core.config import Settings
from catalog.services import table_claims
from catalog.services.table_claims import ResolvedTable
from service_kit.control_emit import NoopControlEmitter
from service_kit.governed import fga
from service_kit.lakehouse import location_claims, protection
from service_kit.lakehouse.naming import UUID_PROPERTY
from tests.unit.no_uuids import NO_OBJECT_UUIDS


def _request_stub() -> Any:
    """The minimum `undrop_namespace` touches on a Request: the per-root connection cache.

    It re-derives its namespace connection from the trash record's binding (diff2 F6 leg c), because
    `NamespaceDep` resolves BEFORE the handler body and — the binding having been removed by the drop
    — has already fallen back to the estate's default root. These tests drive the handler directly
    rather than over HTTP, so they supply the one attribute that path reads.
    """
    from types import SimpleNamespace

    from catalog.core.namespace import NamespaceConnections

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(connections=NamespaceConnections())))


#: The root the stand-in backend lays its tables out under (:func:`_location`).
#: A local root nothing is stored at: a destroy finds no byte to delete, and no store is reached.
_ROOT = "/nonexistent/bkt"


def _resolver(settings: Settings, ns: Any, monkeypatch: pytest.MonkeyPatch) -> Resolver:
    """The catalog's own request resolver, routing every name to the stand-in backend ``ns``, as the lifespan's estate
    connection would."""
    from starlette.requests import Request

    from catalog.api import dependencies

    async def _estate(*_a: Any, **_k: Any) -> Any:
        return ns

    monkeypatch.setattr(dependencies, "estate_namespace", _estate)
    return Resolver(Request({"type": "http", "query_string": b"", "headers": []}), settings)


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


def _uuid(canonical: str) -> str:
    """The uuid the catalog minted for the object named ``canonical`` (ADR 0173 §2).

    Derived from the name only so a stand-in backend can answer it without a store; every record is keyed by it, never
    by the name.
    """
    return str(uuid5(NAMESPACE_URL, canonical))


def _location(segments: list[str]) -> str:
    return f"{_ROOT}/{'_'.join(segments)}.lance"


def _stand(settings: Settings, *tables: str) -> None:
    """Each table's location claim, recording its uuid, as the door that made the table took it."""
    store = table_claims.claim_store(settings)
    for canonical in tables:
        segments = canonical.split("$")
        location_claims.take(store, _location(segments), canonical, segments, is_gone=lambda _claim: False, uuid=_uuid(canonical))


class _RecordingNamespace:
    """Counts native calls — a refused door must never reach the backend.

    It describes a namespace with the ``rask.uuid`` property and a table at its location, as the real backend answers
    for objects the catalog's doors made.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def describe_table(self, request: Any) -> Any:
        return type("R", (), {"location": _location(request.id), "is_only_declared": False, "model_fields_set": set()})()

    def describe_namespace(self, request: Any) -> Any:
        return type("R", (), {"properties": {UUID_PROPERTY: _uuid("$".join(request.id))}, "model_fields_set": set()})()

    def deregister_table(self, request: Any) -> Any:
        self.calls.append("deregister_table")
        return DeregisterTableResponse(id=request.id, location=_location(request.id))

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
        {"kind": kind, "id": canonical, "uuid": _uuid(canonical), "protected": "true", "set_by": "test"},
    )


# ---------------------------------------------------------------- the store


def test_protection_roundtrip_and_absence_means_unprotected(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    so = settings.storage_options()
    pages = _uuid("bronze$pages")
    assert protection.get_protection(settings.registry_root, so, "table", pages) is None
    _protect(settings, "table", "bronze$pages")
    record = protection.get_protection(settings.registry_root, so, "table", pages)
    assert record is not None and record["protected"] == "true"
    assert protection.get_protection(settings.registry_root, so, "table", "bronze$pages") is None, "a record is keyed by the uuid, never the name"
    assert protection.clear_protection(settings.registry_root, so, "table", pages) is True
    assert protection.get_protection(settings.registry_root, so, "table", pages) is None
    assert protection.clear_protection(settings.registry_root, so, "table", pages) is False


def test_kinds_do_not_collide(tmp_path: Any) -> None:
    """A namespace and a table sharing a key are separate records."""
    settings = _settings(tmp_path)
    so = settings.storage_options()
    _protect(settings, "table", "bronze")
    assert protection.get_protection(settings.registry_root, so, "namespace", _uuid("bronze")) is None


# ---------------------------------------------------------------- the guard contract


# ---------------------------------------------------------------- the doors (shipped handlers)


def _resolved(settings: Settings) -> ResolvedTable:
    """The table the door's name resolved to, as the router's resolution hands it to the handler."""
    _stand(settings, "bronze$pages")
    location = _location(["bronze", "pages"])
    return ResolvedTable(segments=["bronze", "pages"], location=location, claim=location_claims.read(table_claims.claim_store(settings), location))


def _drop_table(settings: Settings, ns: Any, *, force: bool = False) -> Any:
    from catalog.api.v1.endpoints import tables as t_ep

    return asyncio.run(
        t_ep.drop_table(
            id="bronze$pages",
            keyed=KeyedDoor(registry_root="", storage_options={}, scope="anonymous", key=None),
            ns=ns,  # structural stand-in, recorded not mocked
            table=_resolved(settings),
            settings=settings,
            client=None,
            emitter=_NoopLineage(),
            control=NoopControlEmitter(),
            token=None,
            so={},
            authorization=None,
            force=force,
            uuids=NO_OBJECT_UUIDS,
        )
    )


def test_a_protected_table_refuses_drop_and_the_native_call_never_ran(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    with pytest.raises(InvalidTableStateError, match="protected against deletion"):
        _drop_table(settings, ns)
    assert ns.calls == [], "the guard must run BEFORE the native call — bytes were touched"


def test_force_drops_a_protected_table_and_the_record_dies_with_it(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    so = settings.storage_options()
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    _drop_table(settings, ns, force=True)
    assert ns.calls == ["deregister_table"]
    assert protection.get_protection(settings.registry_root, so, "table", _uuid("bronze$pages")) is None, "the record must die with its table"


def test_a_protected_table_refuses_deregister_too(tmp_path: Any) -> None:
    """Deregister keeps bytes but removes the object from GOVERNANCE — leaving it ungated would make
    'deregister, then delete the files by hand' the unprotected path around the protected drop."""
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path)
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    with pytest.raises(InvalidTableStateError, match="protected against deletion"):
        asyncio.run(
            t_ep.deregister_table(
                id="bronze$pages",
                keyed=KeyedDoor(registry_root="", storage_options={}, scope="anonymous", key=None),
                ns=ns,
                table=_resolved(settings),
                settings=settings,
                client=None,
                emitter=_NoopLineage(),
                control=NoopControlEmitter(),
                token=None,
                so={},
                authorization=None,
                uuids=NO_OBJECT_UUIDS,
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
                emitter=cast(Any, None),
                body=None,
                uuids=NO_OBJECT_UUIDS,
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
            table=_resolved(settings),
            settings=settings,
            token=None,
            control=NoopControlEmitter(),
        )
    )
    assert result.protected is True
    assert (protection.get_protection(settings.registry_root, so, "table", _uuid("bronze$pages")) or {})["set_by"] == "anonymous"
    result = asyncio.run(
        t_ep.set_table_protection(
            id="bronze$pages",
            body=t_ep.SetProtectionRequest(protected=False),
            table=_resolved(settings),
            settings=settings,
            token=None,
            control=NoopControlEmitter(),
        )
    )
    assert result.protected is False
    assert protection.get_protection(settings.registry_root, so, "table", _uuid("bronze$pages")) is None


def test_the_protection_suffix_is_owner_gated_not_writer(tmp_path: Any) -> None:
    """The authz map is where each door's tier is declared, and a misplaced entry grants the wrong rung
    — pin both kinds.

    THE TIER IS THE ASSERTION, not the relation's NAME. This asserted `== "can_drop"` / `== "can_delete"`,
    which reads as the same thing and is not: it pins the spelling and says nothing about the rung, so it
    would pass over a `can_drop` re-tiered to `writer` and fail over a correctly owner-tier rung that
    happens to be called something else. `can_set_protection` is exactly that second case ([[LH-055]]) —
    its own ACTION over the drop's TIER, so `#41`'s audit trail can tell arming a safety from destroying
    the object. Resolved against the compiled model, which is where the rung actually lives.
    """
    model = {t["type"]: (t.get("relations") or {}) for t in fga.load_model()["type_definitions"]}
    for fga_type in ("table", "namespace"):
        relation = fga_deps._OWNER_SUFFIX_RELATION[fga_type]["protection"]  # noqa: SLF001
        definition = model[fga_type].get(relation)
        assert definition is not None, f"the {fga_type} protection door checks {relation!r}, which the model does not define"
        assert definition == {"computedUserset": {"relation": "owner"}}, (
            f"{fga_type}#{relation} is {definition!r}, not the OWNER tier — a protection door below owner "
            f"lets a writer disarm a safety guarding a destruction they could never perform."
        )


# ---------------------------------------------------------------- #75 trash / undrop


def test_expired_selects_only_past_deadlines_and_never_deletes(tmp_path: Any) -> None:
    """``expired`` is a SELECTION. It is the report half of a reclaimer whose false positive costs a
    table someone was still inside their window to recover."""
    from datetime import UTC, datetime, timedelta

    from service_kit.lakehouse import trash

    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    fresh = trash.make_record("ns$fresh", uuid=_uuid("ns$fresh"), location="s3://b/fresh", dropped_by="u", grace_days=7, now=now)
    old = trash.make_record("ns$old", uuid=_uuid("ns$old"), location="s3://b/old", dropped_by="u", grace_days=7, now=now - timedelta(days=30))
    due = trash.expired([fresh, old], now=now)
    assert [r["id"] for r in due] == ["ns$old"]


def test_an_undated_or_unparseable_record_is_NOT_expired(tmp_path: Any) -> None:
    """ "We do not know when this expires" must read as "not yet" — the same fail-toward-not-deleting
    stance the sweep takes when the policy registry is unreadable."""
    from service_kit.lakehouse import trash

    assert trash.expired([{"id": "a"}, {"id": "b", "expires_at": "not-a-date"}]) == []


class _TrashableNamespace(_RecordingNamespace):
    """Adds the describe/deregister/register trio the #75 drop→undrop path drives."""

    def describe_table(self, request: Any) -> Any:
        self.calls.append("describe_table")
        return super().describe_table(request)

    def register_table(self, request: Any) -> Any:
        self.calls.append(f"register_table:{request.location}")
        return type("R", (), {"location": request.location, "model_fields_set": set()})()


def test_purge_true_still_destroys_immediately(tmp_path: Any) -> None:
    """The explicit opt-out: a caller who means 'destroy the bytes now' says so, and no trash is filed."""
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    asyncio.run(
        __import__("catalog.api.v1.endpoints.tables", fromlist=["tables"]).drop_table(
            id="bronze$pages",
            keyed=KeyedDoor(registry_root="", storage_options={}, scope="anonymous", key=None),
            ns=ns,
            table=_resolved(settings),
            settings=settings,
            client=None,
            emitter=_NoopLineage(),
            control=NoopControlEmitter(),
            token=None,
            so={},
            authorization=None,
            force=False,
            purge=True,
            uuids=NO_OBJECT_UUIDS,
        )
    )
    assert "deregister_table" in ns.calls
    assert trash.list_all(settings.registry_root, settings.storage_options()) == []


def test_undrop_without_a_record_is_an_honest_404(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired or never-trashed drop is genuinely unrecoverable — say so, rather than a 200 that
    recovers nothing."""
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _TrashableNamespace()
    with pytest.raises(TableNotFoundError, match="no recoverable drop"):
        asyncio.run(
            t_ep.undrop_table(
                id="bronze$gone",
                ns=ns,
                root=_ROOT,
                settings=settings,
                token=None,
                control=NoopControlEmitter(),
                emitter=_NoopLineage(),
                uuids=NO_OBJECT_UUIDS,
                resolver=_resolver(settings, ns, monkeypatch),
            )
        )


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
    deps.revoke_ownership = revoked
    try:
        _drop_table(settings, ns)
    finally:
        deps.revoke_ownership = original
    revoked.assert_not_awaited()


def test_a_CASCADE_refuses_when_a_descendant_is_protected(tmp_path: Any) -> None:
    """A cascade destroys children INSIDE one native call — they never reach a door. Before this, a
    protected table under a cascade-dropped namespace died silently while the docstring claimed
    otherwise. The refusal must NAME the protected descendant: "something in here is protected" is
    not an answer anyone can act on.

    IT REFUSES AS A TABLE (code 19) THOUGH THE DOOR IS A NAMESPACE'S, and the cascade is where that
    matters most. Here the namespace genuinely IS non-empty, so `NamespaceNotEmpty` would be a
    well-founded instruction: a generated client would empty the container and retry, dropping every
    unprotected sibling on the way, and still never pass the one protected table. The code names the
    OBSTRUCTION, not the door."""
    from lance_namespace import DropNamespaceRequest

    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path)
    _stand(settings, "bronze$pages")
    _protect(settings, "table", "bronze$pages")
    ns: Any = _RecordingNamespace()
    with pytest.raises(InvalidTableStateError, match="bronze\\$pages"):
        asyncio.run(
            n_ep.drop_namespace(
                id="bronze",
                ns=ns,
                settings=settings,
                token=None,
                client=None,
                control=NoopControlEmitter(),
                emitter=cast(Any, None),
                body=DropNamespaceRequest(behavior="Cascade"),
                uuids=NO_OBJECT_UUIDS,
            )
        )
    assert ns.calls == [], "the cascade ran despite a protected descendant"


def test_force_lets_a_cascade_through(tmp_path: Any) -> None:
    """The negative twin — force turns the protection lock on the subtree exactly as at the named rung."""
    from lance_namespace import DropNamespaceRequest

    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path)
    _stand(settings, "bronze$pages")
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
            emitter=cast(Any, None),
            body=DropNamespaceRequest(behavior="Cascade"),
            force=True,
            uuids=NO_OBJECT_UUIDS,
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
        return _RecordingNamespace.describe_table(self, request)

    def deregister_table(self, request: Any) -> Any:
        self.calls.append(f"deregister_table:{'$'.join(request.id)}")
        return DeregisterTableResponse(id=request.id, location=_location(request.id))

    def drop_namespace(self, request: Any) -> Any:
        self.calls.append(f"drop_namespace:{'$'.join(request.id)}:{(request.behavior or 'restrict').lower()}")
        return type("R", (), {"model_fields_set": set()})()

    def create_namespace(self, request: Any) -> Any:
        self.calls.append(f"create_namespace:{'$'.join(request.id)}:{(request.mode or 'create').lower()}")
        return type("R", (), {"model_fields_set": set()})()


def _drop_namespace_cascade(settings: Settings, ns: Any, *, force: bool = False, purge: bool = False) -> Any:
    from lance_namespace import DropNamespaceRequest

    from catalog.api.v1.endpoints import namespaces as n_ep

    _stand(settings, "bronze$pages", "bronze$inner$t2")
    return asyncio.run(
        n_ep.drop_namespace(
            id="bronze",
            ns=ns,
            settings=settings,
            token=None,
            client=None,
            control=NoopControlEmitter(),
            emitter=cast(Any, None),
            body=DropNamespaceRequest(behavior="Cascade"),
            force=force,
            purge=purge,
            uuids=NO_OBJECT_UUIDS,
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

    filed = {(record["kind"], record["id"]): record for record in trash.list_all(settings.registry_root, settings.storage_options())}
    assert sorted(filed) == [("namespace", "bronze"), ("namespace", "bronze$inner"), ("table", "bronze$inner$t2"), ("table", "bronze$pages")]
    assert filed[("table", "bronze$pages")]["location"] == _location(["bronze", "pages"])
    # Each record is its object's, and names the cascade that filed it by the dropped namespace's uuid.
    assert {key: (record["uuid"], record["cascade"]) for key, record in filed.items()} == {(kind, name): (_uuid(name), _uuid("bronze")) for kind, name in filed}


def test_cascade_purge_true_destroys_and_files_nothing(tmp_path: Any) -> None:
    """The explicit opt-out, same word as the table door: purge means destroy the bytes now, with no trash record.

    It destroys BOTTOM-UP rather than forwarding `behavior=Cascade`: the `dir` backend the chart runs
    does not implement the native cascade at all (#117). Which bytes a destroy deletes is pinned over moto
    (`tests/integration/test_moto_s3.py::test_a_destructive_drop_leaves_no_byte_of_the_table_at_its_location`).
    """
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _CascadableNamespace()
    _drop_namespace_cascade(settings, ns, purge=True)
    assert "drop_namespace:bronze:restrict" in ns.calls  # the root, once every child is gone
    assert not any(c.endswith(":cascade") for c in ns.calls)
    # Each table's row goes by deregister, and a residue record lives only while its bytes are deleted ([[LH-532]]).
    assert trash.list_all(settings.registry_root, settings.storage_options()) == [], "a destructive cascade left a trash record behind it"


def test_namespace_undrop_rebuilds_the_whole_subtree(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plural undrop: namespaces shallowest-first, then every table re-registered at its old id
    from the RELATIVE location form (#75's dir-backend lesson), and the records cleared."""
    from catalog.api.v1.endpoints import namespaces as n_ep
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _CascadableNamespace()
    _drop_namespace_cascade(settings, ns)
    ns.calls.clear()

    asyncio.run(
        n_ep.undrop_namespace(
            id="bronze",
            request=_request_stub(),
            ns=ns,
            root=_ROOT,
            settings=settings,
            token=None,
            control=NoopControlEmitter(),
            emitter=_NoopLineage(),
            uuids=NO_OBJECT_UUIDS,
            resolver=_resolver(settings, ns, monkeypatch),
        )
    )

    creates = [c for c in ns.calls if c.startswith("create_namespace:")]
    assert creates == ["create_namespace:bronze:exist_ok", "create_namespace:bronze$inner:exist_ok"], "parents must exist before children"
    assert "register_table:bronze_pages.lance" in ns.calls
    assert "register_table:bronze_inner_t2.lance" in ns.calls
    assert ns.calls.index(creates[1]) < ns.calls.index("register_table:bronze_inner_t2.lance")
    assert trash.list_all(settings.registry_root, settings.storage_options()) == [], "a recovered record must be cleared"


def test_namespace_undrop_without_a_record_is_an_honest_404(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from lance_namespace import NamespaceNotFoundError

    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _CascadableNamespace()  # structural stand-in, same as every door test here
    with pytest.raises(NamespaceNotFoundError, match="no recoverable drop"):
        asyncio.run(
            n_ep.undrop_namespace(
                id="bronze",
                request=_request_stub(),
                ns=ns,
                root=_ROOT,
                settings=settings,
                token=None,
                control=NoopControlEmitter(),
                emitter=_NoopLineage(),
                uuids=NO_OBJECT_UUIDS,
                resolver=_resolver(settings, ns, monkeypatch),
            )
        )


def test_namespace_tasks_reports_the_pending_expiry(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deadline the owner is racing must be visible on the namespace rung too, with the uuid an undrop names it by."""
    from catalog.api.v1.endpoints import namespaces as n_ep

    settings = _settings(tmp_path, grace_days=7)
    ns: Any = _CascadableNamespace()
    assert asyncio.run(n_ep.namespace_tasks(id="bronze", resolver=_resolver(settings, ns, monkeypatch))) == []
    _drop_namespace_cascade(settings, ns)
    entries = asyncio.run(n_ep.namespace_tasks(id="bronze", resolver=_resolver(settings, ns, monkeypatch)))
    assert [entry.uuid for entry in entries] == [_uuid("bronze")] and entries[0].expires_at, "the undrop deadline is invisible"


def test_trash_kinds_do_not_collide(tmp_path: Any) -> None:
    """A namespace and a table sharing a canonical id are separate trash records — same rule the
    protection store already pins."""
    from service_kit.lakehouse import trash

    settings = _settings(tmp_path)
    so = settings.storage_options()
    trash.put(settings.registry_root, so, trash.make_record("bronze", uuid=_uuid("bronze"), location="s3://b/t", dropped_by="u", grace_days=7))
    assert trash.get(settings.registry_root, so, _uuid("bronze"), kind="namespace") is None
    assert trash.get(settings.registry_root, so, _uuid("bronze")) is not None


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
    (record,) = trash.named(settings.registry_root, so, "bronze", kind="namespace")
    assert record.get("binding") == {"warehouse_id": "wh1", "root_uri": "s3://wh1-bucket"}, "the route was lost, not moved"


def test_the_protection_flag_is_READABLE_on_both_rungs(tmp_path: Any) -> None:
    """#123. The SET door shipped with no read at all — an operator could not arm-check, disarm-check,
    or audit protection; they discovered it by eating a 409. The GET returns the flag AND who armed it."""
    from catalog.api.v1.endpoints import namespaces as n_ep
    from catalog.api.v1.endpoints import tables as t_ep

    settings = _settings(tmp_path)
    so = settings.storage_options()
    ns: Any = _RecordingNamespace()
    _stand(settings, "db1$t", "db1$other")
    store = table_claims.claim_store(settings)
    protection.set_protection(settings.registry_root, so, {"kind": "table", "id": "db1$t", "uuid": _uuid("db1$t"), "protected": "true", "set_by": "user:alice"})

    read = asyncio.run(t_ep.get_table_protection("db1$t", table=table_claims.resolve(ns, store, ["db1", "t"]), settings=settings))
    assert (read.protected, read.set_by) == (True, "user:alice")
    unarmed = asyncio.run(t_ep.get_table_protection("db1$other", table=table_claims.resolve(ns, store, ["db1", "other"]), settings=settings))
    assert (unarmed.protected, unarmed.set_by) == (False, None)

    protection.set_protection(
        settings.registry_root, so, {"kind": "namespace", "id": "bronze", "uuid": _uuid("bronze"), "protected": "true", "set_by": "user:bob"}
    )
    ns_read = asyncio.run(n_ep.get_namespace_protection("bronze", ns=ns, settings=settings))
    assert (ns_read.protected, ns_read.set_by) == (True, "user:bob")
