"""#3-A — warehouse registry, runtime bucket provisioning, and the admin FGA gate (unit).

The registry round-trips against a LOCAL filesystem root (``service_kit.lakehouse.objectfs.fs_and_base`` falls back to
the local FS for a non-``s3://`` uri), so no object storage is needed here. Provisioning is boto3,
mocked. The gate is driven through the real ``require_can_create_warehouse`` resolver, and a separate test
asserts the (type, relation)
it checks actually EXISTS in the compiled model (the mocked-``fga.check`` caveat: a mock pins the passed
string, not that the relation is defined on the type).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from catalog.api import dependencies, fga_deps
from catalog.core.config import Settings
from catalog.services import projects as proj_svc
from catalog.services import warehouses
from lance_namespace import InvalidInputError, PermissionDeniedError, ServiceUnavailableError, TableNotFoundError

from service_kit.governed import fga as fga_module
from service_kit.governed.oidc import IDToken


def _root(tmp_path: Any) -> str:
    return f"file://{tmp_path}"


# --------------------------------------------------------------------------- #
# registry round-trip
# --------------------------------------------------------------------------- #


def test_put_get_list_roundtrip(tmp_path: Any) -> None:
    root, so = _root(tmp_path), {}
    rec_a = {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "created_at": "t"}
    rec_b = {"id": "wh-b", "bucket": "bkt-b", "root_uri": "s3://bkt-b", "project": "acme", "created_at": "t"}
    assert warehouses.get_warehouse(root, so, "wh-a") is None  # unregistered → None
    warehouses.put_warehouse(root, so, rec_a)
    warehouses.put_warehouse(root, so, rec_b)
    assert warehouses.get_warehouse(root, so, "wh-a") == rec_a
    assert {r["id"] for r in warehouses.list_warehouses(root, so)} == {"wh-a", "wh-b"}


def test_list_empty_registry_is_empty(tmp_path: Any) -> None:
    assert warehouses.list_warehouses(_root(tmp_path), {}) == []


def test_list_skips_corrupt_and_malformed_records(tmp_path: Any) -> None:
    # Per-record tolerance (audit 2026-07-23 minor): one bad registry object must never void the listing
    # (it feeds the policy set + the bucket-claim guards) — skip with a warning, keep the readable rest.
    root, so = _root(tmp_path), {}
    good = {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "created_at": "t"}
    warehouses.put_warehouse(root, so, good)
    (tmp_path / "_warehouses" / "zzz-corrupt.json").write_text("{truncated")
    (tmp_path / "_warehouses" / "zzz-notdict.json").write_text('["not", "a", "record"]')
    (tmp_path / "_warehouses" / "zzz-idless.json").write_text('{"bucket": "x", "project": "p"}')
    assert warehouses.list_warehouses(root, so) == [good]


def test_projects_claiming_bucket() -> None:
    records = [
        {"id": "wh-a", "bucket": "shared", "project": "acme"},
        {"id": "wh-b", "bucket": "shared", "project": "evil"},
        {"id": "wh-c", "bucket": "other", "project": "acme"},
        {"id": "wh-d", "bucket": "shared", "project": "acme"},  # duplicate claim collapses per project
    ]
    assert warehouses.projects_claiming_bucket(records, "shared") == {"acme", "evil"}
    assert warehouses.projects_claiming_bucket(records, "other") == {"acme"}
    assert warehouses.projects_claiming_bucket(records, "ghost") == set()


def test_bind_and_resolve(tmp_path: Any) -> None:
    root, so = _root(tmp_path), {}
    assert warehouses.warehouse_for_namespace(root, so, "team_ns") is None  # unbound → default root
    warehouses.bind_namespace(root, so, "team_ns", "wh-a", "s3://bkt-a")
    assert warehouses.warehouse_for_namespace(root, so, "team_ns") == "s3://bkt-a"


# --------------------------------------------------------------------------- #
# provisioning (idempotent, like `mc mb --ignore-existing`)
# --------------------------------------------------------------------------- #

_SO = {"endpoint": "http://rf:9000", "access_key_id": "k", "secret_access_key": "s", "region": "us-east-1"}


def test_provision_creates_bucket() -> None:
    fake = MagicMock()
    with patch("boto3.client", return_value=fake):
        warehouses.provision_bucket("bkt-a", _SO)
    fake.create_bucket.assert_called_once_with(Bucket="bkt-a")  # us-east-1 → no LocationConstraint


def test_provision_adds_location_constraint_off_us_east_1() -> None:
    # Real AWS S3 rejects create_bucket without a LocationConstraint outside us-east-1 (RustFS ignores it).
    fake = MagicMock()
    with patch("boto3.client", return_value=fake):
        warehouses.provision_bucket("bkt-a", {**_SO, "region": "eu-west-1"})
    fake.create_bucket.assert_called_once_with(Bucket="bkt-a", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})


@pytest.mark.parametrize("code", ["BucketAlreadyOwnedByYou", "BucketAlreadyExists"])
def test_provision_idempotent_on_existing(code: str) -> None:
    fake = MagicMock()
    fake.create_bucket.side_effect = ClientError({"Error": {"Code": code}}, "CreateBucket")
    with patch("boto3.client", return_value=fake):
        warehouses.provision_bucket("bkt-a", _SO)  # must NOT raise — a re-provision is a no-op


def test_provision_reraises_real_errors() -> None:
    fake = MagicMock()
    fake.create_bucket.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "CreateBucket")
    with patch("boto3.client", return_value=fake), pytest.raises(ClientError):
        warehouses.provision_bucket("bkt-a", _SO)


# --------------------------------------------------------------------------- #
# the admin gate — driven through the REAL resolver
# --------------------------------------------------------------------------- #


def _fga_settings(control_root: str | None = None) -> Settings:
    data: dict[str, object] = {
        "fga_enabled": True,
        "oidc_enabled": True,
        "oidc_issuer": "https://idp",
        "oidc_audience": "lance",
        "s3_access_key_id": "x",
        "s3_secret_access_key": "x",
    }
    if control_root is not None:
        data["control_root"] = control_root
    return Settings.model_validate(data)


_TOKEN = IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)


def test_gate_checks_can_create_warehouse_on_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[str, str, str]] = []

    async def fake_check(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        captured.append((user, relation, obj))
        return True

    monkeypatch.setattr(fga_module, "check", fake_check)
    asyncio.run(fga_deps.require_can_create_warehouse(MagicMock(), _fga_settings(), _TOKEN, project="acme"))
    # The gate must check the DORMANT project-admin action on the PROJECT, not a table/namespace relation.
    assert captured == [("alice", "can_create_warehouse", "project:acme")]


def test_gate_denies_with_403(monkeypatch: pytest.MonkeyPatch) -> None:
    async def deny(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        return False

    monkeypatch.setattr(fga_module, "check", deny)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps.require_can_create_warehouse(MagicMock(), _fga_settings(), _TOKEN, project="acme"))


def test_gate_fails_closed_on_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenFGA outage is a 503, never a fall-through: no decision is not a denial, and since the
    bootstrap exception died there is no second door for it to leak into either."""

    async def outage(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        raise ServiceUnavailableError("openfga down")

    monkeypatch.setattr(fga_module, "check", outage)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(fga_deps.require_can_create_warehouse(MagicMock(), _fga_settings(), _TOKEN, project="acme"))


def test_gate_noop_when_fga_off() -> None:
    off = Settings.model_validate({"s3_access_key_id": "x", "s3_secret_access_key": "x"})
    asyncio.run(fga_deps.require_can_create_warehouse(None, off, None, project="acme"))  # no raise, no client


def test_model_actually_defines_the_warehouse_relations() -> None:
    # The mocked-fga caveat: a mock pins the STRING, not that the relation EXISTS on the type. Assert the
    # (type, relation) pairs this feature checks are really in the compiled model, so a phantom relation
    # (which OpenFGA rejects with a 400 → fail-closed 503 for every caller) is caught here, not in prod.
    model = fga_module.load_model()
    defined = {(t["type"], rel) for t in model["type_definitions"] for rel in (t.get("relations") or {})}
    assert ("project", "can_create_warehouse") in defined
    assert ("warehouse", "can_get_metadata") in defined
    assert ("warehouse", "can_create_namespace") in defined
    # The first-warehouse bootstrap door + the tenant-admin grant it seeds.
    assert ("warehouse", "can_observe_events") in defined
    assert ("project", "admin") in defined


def test_the_ownership_seeds_write_only_relations_the_model_defines(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard for a WRITE-side model mismatch the mocked-`fga.check` gate tests cannot catch: the
    # seed originally wrote `warehouse#parent` (via grant_on_create) but the warehouse type's parent-pointer
    # is `project`, not `parent` — OpenFGA rejects the undefined relation and the whole seed 503s LIVE while
    # every unit test stays green. Drive the REAL seed with a recording `write_tuples` and assert every
    # (type, relation) it emits is actually defined in the compiled model.json.
    captured: list[Any] = []

    async def rec_write(_client: object, tuples: list[Any], **_kw: object) -> None:
        captured.extend(tuples)

    monkeypatch.setattr(fga_module, "write_tuples", rec_write)
    # Drive BOTH seeds: together they are the full tuple surface this feature writes — seed_warehouse
    # (owner + the project edge, from warehouse-create) and seed_project_admin (the tenant's admin,
    # minted by POST /v1/projects since the bootstrap exception died).
    asyncio.run(fga_deps.seed_warehouse(MagicMock(), _fga_settings(), _TOKEN, warehouse_id="wh-a", project="acme"))
    granted = asyncio.run(fga_deps.seed_project_admin(MagicMock(), _fga_settings(), _TOKEN, project="acme"))
    assert granted is True  # reported, so create_project only audits a grant that really happened
    assert captured, "the seeds wrote no tuples"
    model = {t["type"]: set(t.get("relations") or {}) for t in fga_module.load_model()["type_definitions"]}
    assignable = {
        (t["type"], rel)
        for t in fga_module.load_model()["type_definitions"]
        for rel, meta in ((t.get("metadata") or {}).get("relations") or {}).items()
        if meta.get("directly_related_user_types")
    }
    for t in captured:
        obj_type = t.object.split(":", 1)[0]
        assert t.relation in model.get(obj_type, set()), (
            f"the seed writes {obj_type}#{t.relation}, NOT a relation on the compiled model — OpenFGA "
            f"would reject the seed (503). {obj_type} has: {sorted(model.get(obj_type, set()))}"
        )
        # Defined is not enough: a DERIVED relation (a `can_*` action) accepts no direct tuple, so writing
        # one is the same OpenFGA 400 → fail-closed 503. Every seeded relation must be directly assignable.
        assert (obj_type, t.relation) in assignable, f"the seed writes {obj_type}#{t.relation}, a DERIVED relation that accepts no direct tuple"
    written = {(t.object.split(":", 1)[0], t.relation) for t in captured}
    assert ("warehouse", "project") in written  # the parent link uses the warehouse's real pointer relation
    assert ("warehouse", "parent") not in written  # ...never the namespace/table `parent` name (the old bug)
    assert ("project", "admin") in written  # the tenant's admin grant, minted at POST /v1/projects


def test_seed_warehouse_never_grants_project_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    # The escalation path is closed STRUCTURALLY: a warehouse create writes exactly the owner grant and
    # the project edge — project admin cannot ride a warehouse create at all any more (it moved to
    # seed_project_admin, behind the estate-gated POST /v1/projects), so a warehouse-owner can never
    # quietly escalate to tenant admin.
    captured: list[Any] = []

    async def rec_write(_client: object, tuples: list[Any], **_kw: object) -> None:
        captured.extend(tuples)

    monkeypatch.setattr(fga_module, "write_tuples", rec_write)
    asyncio.run(fga_deps.seed_warehouse(MagicMock(), _fga_settings(), _TOKEN, warehouse_id="wh-a", project="acme"))
    assert {(t.user, t.relation, t.object) for t in captured} == {
        (f"user:{_TOKEN.sub}", "owner", "warehouse:wh-a"),
        ("project:acme", "project", "warehouse:wh-a"),
    }


# --------------------------------------------------------------------------- #
# tenant minting (Decision 1) — a tenant is born at POST /v1/projects: the estate door
# writes the registry record + the creator's project#admin tuple in ONE operation, and
# warehouse-create keeps a single door (the project's own rung, no bootstrap exception).
# --------------------------------------------------------------------------- #

#: The fixed FGA root object every estate-admin gate (/v1/events, /v1/access) checks against — the same
#: default `Settings.fga_root_object` carries and `chart/templates/bootstrap-admin.yaml` grants `owner` on.
_ROOT_OBJECT = "warehouse:lance_catalog"


class _FakeStore:
    """A minimal stand-in for the OpenFGA store: it records the tuples the seed writes and resolves the two
    relations this flow checks against them — ``project#can_create_warehouse`` from a direct ``project#admin``
    tuple and ``warehouse#can_observe_events`` from a direct ``owner`` on the root — both exactly as the
    compiled model derives them. That makes the SEQUENCE provable offline: an estate admin mints a tenant,
    and the tenant is then self-governing (the second warehouse passes on its OWN project rung, not the
    estate door). A relation outside the pair is an error, so a silent gate change can't slip past."""

    def __init__(self, *, estate_admins: tuple[str, ...] = ()) -> None:
        self.tuples: set[tuple[str, str, str]] = {(f"user:{sub}", "owner", _ROOT_OBJECT) for sub in estate_admins}
        self.checks: list[tuple[str, str, str]] = []

    async def check(self, _client: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        self.checks.append((user, relation, obj))
        # The real `fga.check` qualifies a bare sub into `user:<sub>` (qualify=True) before it reaches the
        # store; mirror that here so a checked subject and a seeded tuple are the same string.
        subject = user if ":" in user else f"user:{user}"
        if relation == "can_observe_events":
            return (subject, "owner", obj) in self.tuples
        if relation == "can_create_warehouse":
            return (subject, "admin", obj) in self.tuples
        raise AssertionError(f"the warehouse-create gate checked an unexpected relation: {relation}")

    async def write(self, _client: object, tuples: list[Any], **_kw: object) -> None:
        self.tuples.update((t.user, t.relation, t.object) for t in tuples)

    async def list_users(self, _client: object, *, relation: str, obj: str, **_kw: object) -> list[str]:
        # `can_administer` derives from a direct `admin` tuple in the compiled model; the projects
        # endpoint's display-only admins lookup resolves the same way here.
        return sorted(u for u, rel, o in self.tuples if o == obj and rel == "admin")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fga_module, "check", self.check)
        monkeypatch.setattr(fga_module, "write_tuples", self.write)
        monkeypatch.setattr(fga_module, "list_users", self.list_users)


def _governed_settings(tmp_path: Any) -> Settings:
    return Settings.model_validate(
        {
            "warehouses_enabled": True,
            "control_root": _root(tmp_path),
            "fga_enabled": True,
            "oidc_enabled": True,
            "oidc_issuer": "https://idp",
            "oidc_audience": "lance",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


def _create_as(settings: Settings, sub: str, body: Any) -> Any:
    from catalog.api.v1.endpoints import warehouses as wh_ep

    from service_kit.control_emit import NoopControlEmitter

    return asyncio.run(
        wh_ep.create_warehouse(
            settings=settings,
            token=IDToken(iss="i", sub=sub, aud="lance", exp=1, iat=1),
            client=MagicMock(),
            control=NoopControlEmitter(),
            body=body,
        )
    )


def _create_project_as(settings: Settings, sub: str, project_id: str) -> Any:
    from catalog.api.v1.endpoints import projects as proj_ep

    from service_kit.control_emit import NoopControlEmitter

    return asyncio.run(
        proj_ep.create_project(
            settings=settings,
            token=IDToken(iss="i", sub=sub, aud="lance", exp=1, iat=1),
            client=MagicMock(),
            control=NoopControlEmitter(),
            body=proj_ep.CreateProjectRequest(id=project_id),
        )
    )


def test_gate_has_one_door_only_and_never_probes_the_estate_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bootstrap exception is GONE (Decision 1): a project admin passes, and the estate root is
    never even probed — tenants are minted at POST /v1/projects, not through a warehouse side door."""
    store = _FakeStore()
    store.tuples.add(("user:alice", "admin", "project:acme"))
    monkeypatch.setattr(fga_module, "check", store.check)
    asyncio.run(fga_deps.require_can_create_warehouse(MagicMock(), _fga_settings(), _TOKEN, project="acme"))
    assert store.checks == [("alice", "can_create_warehouse", "project:acme")]


def test_gate_denies_an_ordinary_user_with_no_estate_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed, and the denial is FINAL: no second door exists to fall through to. An estate admin
    who is not this project's admin is denied here too — their path is POST /v1/projects, which seeds
    them as admin, after which this gate passes on the project's own rung."""
    store = _FakeStore(estate_admins=("alice",))  # estate rung held — and still irrelevant here
    monkeypatch.setattr(fga_module, "check", store.check)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps.require_can_create_warehouse(MagicMock(), _fga_settings(), _TOKEN, project="acme"))
    assert store.checks == [("alice", "can_create_warehouse", "project:acme")]


def test_require_project_exists_refuses_an_unknown_tenant(tmp_path: Path) -> None:
    """The guard that REPLACED the bootstrap exception: an unknown project is a 404 (spec code 4,
    problem body) whose detail names the fix — POST /v1/projects. Existence comes from the registry,
    identically for every caller, which is why it runs before authz."""
    settings = _fga_settings(control_root=f"file://{tmp_path}")
    with pytest.raises(TableNotFoundError) as exc:
        asyncio.run(fga_deps.require_project_exists(settings, "ghost"))
    assert "POST /v1/projects" in str(exc.value)
    # …and an existing record passes untouched.
    proj_svc.put_project(f"file://{tmp_path}", {}, {"id": "acme", "created_at": "t", "created_by": "x", "protected": "false"})
    asyncio.run(fga_deps.require_project_exists(settings, "acme"))


def test_estate_admin_creates_a_project_and_the_project_then_governs_itself(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Decision 1 end to end: the estate admin mints the TENANT at POST /v1/projects (the estate door,
    # can_observe_events on the root) — record + her project#admin tuple in one operation — and every
    # warehouse after that passes on the project's OWN rung. The warehouse door never probes the estate.
    from catalog.schemas import CreateWarehouseRequest

    store = _FakeStore(estate_admins=("alice",))
    store.install(monkeypatch)
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: None)
    settings = _governed_settings(tmp_path)

    minted = _create_project_as(settings, "alice", "acme")
    assert store.checks == [("alice", "can_observe_events", _ROOT_OBJECT)]
    assert ("user:alice", "admin", "project:acme") in store.tuples
    assert minted.admins == ["user:alice"]  # existence and permission born together, and the response shows it

    store.checks.clear()
    first = _create_as(settings, "alice", CreateWarehouseRequest(id="acme-wh", project="acme"))
    assert first.id == "acme-wh"
    assert ("user:alice", "owner", "warehouse:acme-wh") in store.tuples
    assert store.checks == [("alice", "can_create_warehouse", "project:acme")]

    store.checks.clear()
    gold = _create_as(settings, "alice", CreateWarehouseRequest(id="acme-gold", project="acme", serving="gold"))
    assert gold.serving == "gold"
    assert store.checks == [("alice", "can_create_warehouse", "project:acme")]


def test_an_ordinary_user_cannot_mint_a_tenant_through_any_door(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail-closed at BOTH doors, before ANY side effect: the project door 403s a non-estate-admin, and
    # the warehouse door 404s the unknown tenant (there is no side door left that would mint it). No
    # bucket, no registry record, no tuple either way.
    from catalog.schemas import CreateWarehouseRequest

    store = _FakeStore(estate_admins=("alice",))
    store.install(monkeypatch)
    provisioned: list[str] = []
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: provisioned.append(bucket))
    settings = _governed_settings(tmp_path)

    with pytest.raises(PermissionDeniedError):
        _create_project_as(settings, "mallory", "evil")
    with pytest.raises(TableNotFoundError):
        _create_as(settings, "mallory", CreateWarehouseRequest(id="evil-wh", project="evil"))
    assert provisioned == []
    assert proj_svc.list_projects(settings.registry_root, {}) == []
    assert warehouses.list_warehouses(settings.registry_root, {}) == []
    assert not any(t[2] == "project:evil" for t in store.tuples)


def test_a_second_warehouse_in_a_live_tenant_still_needs_that_tenants_admin(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Estate rank does not reach INSIDE a live tenant: carol holds the estate door (she could mint her
    # own projects) but is not acme's admin, so acme refuses her warehouse — and no project-admin
    # tuple is minted behind the refusal.
    from catalog.schemas import CreateWarehouseRequest

    store = _FakeStore(estate_admins=("alice", "carol"))
    store.install(monkeypatch)
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: None)
    settings = _governed_settings(tmp_path)
    _create_project_as(settings, "alice", "acme")
    _create_as(settings, "alice", CreateWarehouseRequest(id="acme-wh", project="acme"))

    with pytest.raises(PermissionDeniedError):
        _create_as(settings, "carol", CreateWarehouseRequest(id="acme-two", project="acme"))
    assert ("user:carol", "admin", "project:acme") not in store.tuples
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-two") is None


# --------------------------------------------------------------------------- #
# routing isolation guards (audit F3/F4)
# --------------------------------------------------------------------------- #


def _bare_request() -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(warehouse_binding_cache={})))


def test_binding_lookup_fails_closed_on_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # F4: a registry READ ERROR (not a legitimately-unbound namespace) must fail CLOSED (503) — never fall
    # through to the default shared bucket, which would create/read a maybe-bound tenant's table in the wrong
    # bucket from a transient fault.
    def boom(*_a: object, **_k: object) -> dict[str, str] | None:
        raise OSError("s3 down")

    monkeypatch.setattr(warehouses, "binding_for_namespace", boom)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(dependencies._resolve_warehouse_root(_bare_request(), _fga_settings(), "db1"))


def test_resolver_refuses_when_warehouse_record_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # AUDIT #7: the fail-closed "missing warehouse record (status None) → refuse" branch had NO test. A bound
    # namespace whose warehouse record has vanished must 403, NOT silently route to the (now orphaned) bucket.
    monkeypatch.setattr(
        warehouses,
        "binding_for_namespace",
        lambda *_a, **_k: {"warehouse_id": "wh-a", "root_uri": "s3://bkt-a"},
    )
    monkeypatch.setattr(warehouses, "warehouse_status", lambda *_a, **_k: None)  # record gone
    with pytest.raises(PermissionDeniedError):
        asyncio.run(dependencies._resolve_warehouse_root(_bare_request(), _fga_settings(), "db1"))


def test_resolver_fails_closed_503_on_status_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # AUDIT #3/#5: a TRANSIENT registry error during the LIVE status read must 503 (fail-closed), symmetric
    # with the binding read — NOT propagate as an unhandled 500 (or, worse, fall through to routing).
    monkeypatch.setattr(
        warehouses,
        "binding_for_namespace",
        lambda *_a, **_k: {"warehouse_id": "wh-a", "root_uri": "s3://bkt-a"},
    )

    def boom(*_a: object, **_k: object) -> str | None:
        raise OSError("registry blip")

    monkeypatch.setattr(warehouses, "warehouse_status", boom)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(dependencies._resolve_warehouse_root(_bare_request(), _fga_settings(), "db1"))


def test_binding_lookup_unbound_routes_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # ...but a clean None (unbound) is NOT an error — it routes to the default root.
    monkeypatch.setattr(warehouses, "binding_for_namespace", lambda *_a, **_k: None)
    assert asyncio.run(dependencies._resolve_warehouse_root(_bare_request(), _fga_settings(), "db1")) is None


def test_batch_guard_rejects_warehouse_bound_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    # F3: the batch routes carry no {id} to route by, so they'd place a warehouse-bound tenant's table in the
    # shared bucket. The guard rejects a batch that references a bound namespace; an unbound one passes.
    on = Settings.model_validate({"warehouses_enabled": True, "s3_access_key_id": "x", "s3_secret_access_key": "x"})
    # bound + ACTIVE (the resolver reads status live, so a bound namespace must resolve past the deactivation
    # gate to reach the batch guard's own rejection).
    monkeypatch.setattr(
        warehouses,
        "binding_for_namespace",
        lambda *_a, **_k: {"warehouse_id": "wh-a", "root_uri": "s3://bkt-a"},
    )
    monkeypatch.setattr(warehouses, "warehouse_status", lambda *_a, **_k: "active")
    with pytest.raises(InvalidInputError):
        asyncio.run(dependencies.assert_no_warehouse_bound_namespace(_bare_request(), on, [["db1", "t1"]]))
    monkeypatch.setattr(warehouses, "binding_for_namespace", lambda *_a, **_k: None)  # unbound → ok
    asyncio.run(dependencies.assert_no_warehouse_bound_namespace(_bare_request(), on, [["db2", "t2"]]))


# --------------------------------------------------------------------------- #
# reserved buckets: EVERY platform-owned bucket, regardless of zoning
# --------------------------------------------------------------------------- #


def test_reserved_bucket_set_folds_in_models_and_multibase_buckets() -> None:
    # The set must cover the shared root, the registry/control bucket, a ZONED model-registry root, and
    # every approved multi-base data bucket — plus the named medallion zone buckets. A warehouse over any
    # of them would hand one project governance of platform (or every tenant's) data there.
    s = Settings.model_validate(
        {
            "root": "s3://lance-catalog",
            "control_root": "s3://lance-control/registry",
            "models_registry_root": "s3://lance-models/medallion/models",
            "multibase_data_bases": "s3://lance-data-1, s3://lance-data-2/prefix",
            "reserved_buckets": "lance-source",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )
    assert s.reserved_bucket_set == frozenset({"lance-catalog", "lance-control", "lance-models", "lance-data-1", "lance-data-2", "lance-source"})


def test_reserved_bucket_set_defaults_collapse_to_the_root_bucket() -> None:
    # Defaults: models_root lives UNDER the catalog root and multibase is off, so nothing extra is
    # reserved — byte-identical to the single-bucket posture. file:// roots contribute no bucket at all.
    s = Settings.model_validate({"s3_access_key_id": "x", "s3_secret_access_key": "x"})
    assert s.reserved_bucket_set == frozenset({"lance-catalog"})
    local = Settings.model_validate({"root": "file:///tmp/cat", "s3_access_key_id": "x", "s3_secret_access_key": "x"})
    assert local.reserved_bucket_set == frozenset()


def test_create_warehouse_rejects_models_and_multibase_buckets(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # End-to-end through the endpoint guard: a warehouse claiming the zoned model-registry bucket or an
    # approved multi-base data bucket is refused BEFORE any bucket/registry/FGA side effect.
    from catalog.api.v1.endpoints import warehouses as wh_ep
    from catalog.schemas import CreateWarehouseRequest

    settings = Settings.model_validate(
        {
            "warehouses_enabled": True,
            "control_root": _root(tmp_path),
            "models_registry_root": "s3://lance-models/medallion/models",
            "multibase_data_bases": "s3://lance-data-1,s3://lance-data-2",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )
    provisioned: list[str] = []
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: provisioned.append(bucket))
    # The project EXISTS (seeded in the registry) so the refusal below is the reserved-bucket guard
    # itself, not require_project_exists' 404 firing first.
    proj_svc.put_project(_root(tmp_path), {}, {"id": "evil", "created_at": "t", "created_by": "x", "protected": "false"})
    for bucket in ("lance-models", "lance-data-1", "lance-data-2", "lance-catalog"):
        with pytest.raises(InvalidInputError):
            asyncio.run(
                wh_ep.create_warehouse(
                    settings=settings,
                    token=None,  # FGA off → the admin gate is a no-op; the reserved guard must still fire
                    client=None,
                    control=cast(Any, None),  # only used after a successful create
                    body=CreateWarehouseRequest(id="wh-evil", project="evil", bucket=bucket),
                )
            )
    assert provisioned == []


# --------------------------------------------------------------------------- #
# serving designation (gold serving warehouse — DECISIONS "Medallion tiers")
# --------------------------------------------------------------------------- #


def _serving_settings(tmp_path: Any) -> Settings:
    return Settings.model_validate(
        {
            "warehouses_enabled": True,
            "control_root": _root(tmp_path),
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


def _create(settings: Settings, body: Any) -> Any:
    from catalog.api.v1.endpoints import warehouses as wh_ep

    from service_kit.control_emit import NoopControlEmitter

    # The project must EXIST now (Decision 1) — mint its registry record first, idempotently, so
    # these unit tests keep their real subject (the warehouse record contract) without re-testing
    # the existence guard, which has its own tests.
    proj_svc.put_project(
        settings.registry_root,
        settings.storage_options(),
        {"id": body.project, "created_at": "t", "created_by": "unit", "protected": "false"},
    )
    return asyncio.run(
        wh_ep.create_warehouse(
            settings=settings,
            token=None,  # FGA off → the admin gate + seed are no-ops (unit scope is the record contract)
            client=None,
            control=NoopControlEmitter(),
            body=body,
        )
    )


def test_create_rejects_unknown_serving(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the class the resolvers know is accepted — an unknown value would mint a record NEITHER
    # project_root nor project_gold_root ever matches (an unroutable warehouse). Rejected before any
    # bucket/registry/FGA side effect.
    from catalog.schemas import CreateWarehouseRequest

    provisioned: list[str] = []
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: provisioned.append(bucket))
    with pytest.raises(InvalidInputError):
        _create(
            _serving_settings(tmp_path),
            CreateWarehouseRequest(id="wh-x", project="acme", serving="silver"),
        )
    assert provisioned == []


def test_create_serving_gold_round_trips(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # serving="gold" lands in the response AND the persisted registry record; a plain create keeps the
    # field ABSENT (not null) — the resolver's "absent = work" contract.
    from catalog.schemas import CreateWarehouseRequest

    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: None)
    settings = _serving_settings(tmp_path)
    gold = _create(settings, CreateWarehouseRequest(id="acme-gold", project="acme", serving="gold"))
    assert gold.serving == "gold"
    work = _create(settings, CreateWarehouseRequest(id="acme-work", project="acme"))
    assert work.serving is None
    gold_rec = warehouses.get_warehouse(settings.registry_root, {}, "acme-gold")
    work_rec = warehouses.get_warehouse(settings.registry_root, {}, "acme-work")
    assert gold_rec is not None and gold_rec["serving"] == "gold"
    assert work_rec is not None and "serving" not in work_rec


def test_recreate_without_serving_does_not_demote_gold(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Idempotent re-create carries serving FORWARD (like status/created_at): a GitOps reconcile
    # re-POSTing the gold warehouse without the field must not silently demote it to a work warehouse —
    # the silver→gold mover would quietly fall back to the work root.
    from catalog.schemas import CreateWarehouseRequest

    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: None)
    settings = _serving_settings(tmp_path)
    _create(settings, CreateWarehouseRequest(id="acme-gold", project="acme", serving="gold"))
    again = _create(settings, CreateWarehouseRequest(id="acme-gold", project="acme"))
    assert again.serving == "gold"
    rec = warehouses.get_warehouse(settings.registry_root, {}, "acme-gold")
    assert rec is not None and rec["serving"] == "gold"


def test_an_unreadable_binding_refuses_rather_than_reporting_an_empty_warehouse(tmp_path: Any) -> None:
    """A binding object the catalog cannot parse must FAIL CLOSED, not vanish from the count.

    `list_warehouses`-style tolerance is right for a LISTING and catastrophic for a DELETE: the
    unreadable object is the only thing that says which warehouse it binds, so skipping it turns
    "this warehouse holds nothing" into a guess — and `DELETE ?purge_bucket=true` acts on that guess
    by destroying the tables of a namespace nobody could enumerate.
    """
    root, so = _root(tmp_path), {}
    warehouses.bind_namespace(root, so, "bronze", "wh-a", "s3://bkt-a")
    assert warehouses.namespaces_bound_to(root, so, "wh-a") == ["bronze"]

    (tmp_path / "_warehouses" / "bindings" / "silver.json").write_text("{ this is not json")
    with pytest.raises(ServiceUnavailableError) as exc:
        warehouses.namespaces_bound_to(root, so, "wh-a")
    # The refusal names the unreadable object — an operator cannot fix what the error will not identify.
    assert "silver.json" in str(exc.value)
    # …and the tolerant listing still serves, because a broken binding must not black out enumeration.
    assert [b["top_ns"] for b in warehouses.list_bindings(root, so)] == ["bronze"]


def test_a_warehouse_created_PROTECTED_actually_refuses_its_delete(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """#123. `require_not_protected` shipped at the warehouse delete door a YEAR before any API could
    arm it — the only protected warehouses were hand-edited registry JSON, while both delete dialogs
    shipped an "Override deletion protection" checkbox for a 409 production could not produce. This
    drives arm-at-create through to the refusal: the checkbox's 409 is now real.
    """
    from catalog.schemas import CreateWarehouseRequest
    from lance_namespace import NamespaceNotEmptyError

    settings = _governed_settings(tmp_path)
    _FakeStore(estate_admins=("alice",)).install(monkeypatch)
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: None)
    _create_project_as(settings, "alice", "acme")
    created = _create_as(settings, "alice", CreateWarehouseRequest(id="safe-wh", project="acme", protected=True))
    assert created.protected is True, "the create dropped the flag — observability half"

    from catalog.api import fga_deps
    from catalog.services import warehouses as wh_svc

    record = wh_svc.get_warehouse(settings.registry_root, settings.storage_options(), "safe-wh")
    assert record is not None and record.get("protected") == "true", "the flag never reached the registry record"
    with pytest.raises(NamespaceNotEmptyError, match="protect"):
        fga_deps.require_not_protected(record, kind="warehouse", obj_id="safe-wh", force=False)
    # force=true turns exactly this lock — the dialogs' override checkbox, now honest.
    fga_deps.require_not_protected(record, kind="warehouse", obj_id="safe-wh", force=True)


def test_an_UNPROTECTED_create_stays_byte_identical_on_the_wire(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default must not stamp `protected: "false"` onto every record — pre-protection records
    stay byte-identical, and the response omits the field (exclude_none)."""
    from catalog.schemas import CreateWarehouseRequest

    settings = _governed_settings(tmp_path)
    _FakeStore(estate_admins=("alice",)).install(monkeypatch)
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: None)
    _create_project_as(settings, "alice", "acme")
    created = _create_as(settings, "alice", CreateWarehouseRequest(id="plain-wh", project="acme"))
    assert created.protected is None

    from catalog.services import warehouses as wh_svc

    record = wh_svc.get_warehouse(settings.registry_root, settings.storage_options(), "plain-wh")
    assert record is not None and "protected" not in record
