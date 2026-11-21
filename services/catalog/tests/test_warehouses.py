"""#3-A — warehouse registry, runtime bucket provisioning, and the admin FGA gate (unit).

The registry round-trips against a LOCAL filesystem root (``lakehouse.storage.objectfs.fs_and_base`` falls back to
the local FS for a non-``s3://`` uri), so no object storage is needed here. Provisioning is boto3,
mocked. The gate is driven through the real ``require_can_create_warehouse`` resolver.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import boto3
import lance_namespace
import pytest
from botocore.exceptions import ClientError
from lance_namespace import InternalError, InvalidInputError, LanceNamespace, PermissionDeniedError, ServiceUnavailableError, TableNotFoundError
from moto.server import ThreadedMotoServer
from openfga_sdk.client import OpenFgaClient
from pydantic import SecretStr
from unfiled_announcer import unfiled

from catalog.api import dependencies
from catalog.api.authz import door_gates, hierarchy, object_grants, warehouse_grants
from catalog.api.security import Caller
from catalog.core.config import Settings
from catalog.core.vending import ModeBVendor
from catalog.services import projects as proj_svc
from catalog.services import warehouses
from lakehouse.governed import fga_ids, fga_model, fga_provision, fga_reads, fga_writes
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from lakehouse.storage.credential import S3KeyPair
from lakehouse.storage.objectfs import lance_storage_options
from lakehouse.storage.options import StorageOptions
from tests import seeding
from tests.integration.store_requests import counted


def _root(tmp_path: Any) -> str:
    return f"file://{tmp_path}"


# --------------------------------------------------------------------------- #
# registry round-trip
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# provisioning (idempotent: an owned bucket is a no-op)
# --------------------------------------------------------------------------- #

_SO = StorageOptions.from_flat({"endpoint": "http://rf:9000", "access_key_id": "k", "secret_access_key": "s", "region": "us-east-1"})


def test_provision_adds_location_constraint_off_us_east_1() -> None:
    # Real AWS S3 rejects create_bucket without a LocationConstraint outside us-east-1 (RustFS ignores it).
    fake = MagicMock()
    with patch("lakehouse.storage.client.s3_client", return_value=fake):
        warehouses.provision_bucket("bkt-a", _SO.with_options({"region": "eu-west-1"}))
    fake.create_bucket.assert_called_once_with(Bucket="bkt-a", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})


@pytest.mark.parametrize("code", ["BucketAlreadyOwnedByYou"])
def test_provision_idempotent_on_existing(code: str) -> None:
    fake = MagicMock()
    fake.create_bucket.side_effect = ClientError({"Error": {"Code": code}}, "CreateBucket")
    with patch("lakehouse.storage.client.s3_client", return_value=fake):
        warehouses.provision_bucket("bkt-a", _SO)  # must NOT raise — a re-provision is a no-op


def test_provision_reraises_real_errors() -> None:
    fake = MagicMock()
    fake.create_bucket.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "CreateBucket")
    with patch("lakehouse.storage.client.s3_client", return_value=fake), pytest.raises(ClientError):
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
        "oidc_idp_id": "dex",
        "oidc_connectors": {"local": "dex"},
        "s3_access_key_id": "x",
        "s3_secret_access_key": "x",
    }
    if control_root is not None:
        data["control_root"] = control_root
    return Settings.model_validate(data)


def _caller(key: str) -> PersonPrincipal:
    principal = Principal.parse(key)
    return PersonPrincipal(principal=principal, claims=IDToken(iss="i", sub=principal.subject, aud="lance", exp=1, iat=1))


def test_gate_fails_closed_on_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenFGA outage is a 503, never a fall-through: no decision is not a denial."""

    async def outage(_c: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        raise ServiceUnavailableError("openfga down")

    monkeypatch.setattr(fga_reads, "check", outage)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(door_gates.require_can_create_warehouse(MagicMock(), _fga_settings(), _TOKEN, project="acme"))


_TOKEN = _caller("dex~alice")


def test_the_ownership_seeds_write_only_relations_the_model_defines(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard for a WRITE-side model mismatch the mocked-`fga_reads.check` gate tests cannot catch: the
    # seed originally wrote `warehouse#parent` (via grant_on_create) but the warehouse type's parent-pointer
    # is `project`, not `parent` — OpenFGA rejects the undefined relation and the whole seed 503s LIVE while
    # every unit test stays green. Drive the REAL seed with a recording `write_tuples` and assert every
    # (type, relation) it emits is actually defined in the compiled model.json.
    captured: list[Any] = []

    async def rec_write(_client: object, tuples: list[Any], **_kw: object) -> None:
        captured.extend(tuples)

    monkeypatch.setattr(fga_writes, "write_tuples", rec_write)
    # Drive BOTH seeds: together they are the full tuple surface this feature writes — seed_warehouse
    # (owner + the project edge, from warehouse-create) and seed_project_admin (the tenant's admin,
    # minted by POST /management/v1/projects since the bootstrap exception died).
    asyncio.run(warehouse_grants.seed_warehouse(MagicMock(), _fga_settings(), _TOKEN, warehouse_id="wh-a", project="acme"))
    granted = asyncio.run(object_grants.seed_project_admin(MagicMock(), _fga_settings(), _TOKEN.subject, project="acme", admin="dex~alice"))
    assert granted is True  # reported, so create_project only audits a grant that really happened
    assert captured, "the seeds wrote no tuples"
    model = {t["type"]: set(t.get("relations") or {}) for t in fga_model.load_model()["type_definitions"]}
    assignable = {
        (t["type"], rel)
        for t in fga_model.load_model()["type_definitions"]
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
    assert ("project", "admin") in written  # the tenant's admin grant, minted at POST /management/v1/projects


# --------------------------------------------------------------------------- #
# tenant minting (Decision 1) — a tenant is born at POST /management/v1/projects: the estate door
# writes the registry record + the creator's project#admin tuple in ONE operation, and
# warehouse-create keeps a single door (the project's own rung, no bootstrap exception).
# --------------------------------------------------------------------------- #


class _BucketCatalogs:
    """The catalog's connections, answering each warehouse bucket's root with a directory catalog on local disk, so the
    create door gives the warehouse its own row there without reaching a store."""

    def __init__(self, base: Path) -> None:
        self._base = base

    def connection(self, _settings: Settings, *, root: str | None = None, endpoint: str | None = None, plane: str = "metadata") -> LanceNamespace:
        assert root is not None and root.startswith("s3://"), f"the create opened {root!r}, not its bucket"
        return lance_namespace.connect("dir", {"root": str(self._base / root.removeprefix("s3://"))})


def _request(settings: Settings) -> Any:
    """A stand-in request carrying the one piece of app state the create touches: the catalog's connections."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(connections=_BucketCatalogs(Path(settings.registry_root.removeprefix("file://"))))))


def _governed_settings(tmp_path: Any) -> Settings:
    return Settings.model_validate(
        {
            "control_root": _root(tmp_path),
            "fga_enabled": True,
            "oidc_enabled": True,
            "oidc_issuer": "https://idp",
            "oidc_audience": "lance",
            "oidc_idp_id": "dex",
            "oidc_connectors": {"local": "dex"},
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


#: The ESTATE ROOT every estate-admin gate (/management/v1/events, /management/v1/access, project creation) checks against,
#: read from the setting: a literal that falls behind `fga_root_object` would seed a grant on an object no gate asks about.
_ROOT_OBJECT = str(Settings.model_fields["fga_root_object"].default)


def _on(store: Any, call: Callable[[OpenFgaClient], Awaitable[Any]]) -> Any:  # noqa: ANN401 — the root conftest's OpenFgaStore and the door's answer
    """Run ``call`` with an OpenFGA client on ``store``, the test's own store on the pinned server, as the catalog holds one."""

    async def _run() -> Any:  # noqa: ANN401 — the door's answer
        async with fga_provision.make_client(store.url, store.store_id, store.model_id, token_file=None) as client:
            return await call(client)

    return asyncio.run(_run())


def _create_as(settings: Settings, sub: str, body: Any, store: Any) -> Any:  # noqa: ANN401 — the root conftest's OpenFgaStore
    from catalog.api.v1.endpoints import warehouses as wh_ep
    from lakehouse.events.control_emit import NoopControlEmitter

    return _on(
        store,
        lambda client: wh_ep.create_warehouse(
            request=_request(settings),
            settings=settings,
            token=_caller(sub),
            client=client,
            control=NoopControlEmitter(),
            vendor=ModeBVendor(),  # the default `get_vendor` falls back to: no direct credentials, so the probe skips
            body=body,
        ),
    )


def _create_project_as(settings: Settings, token: Caller, body: Any, store: Any) -> Any:  # noqa: ANN401 — the request body and the root conftest's OpenFgaStore
    from catalog.api.v1.endpoints import projects as proj_ep
    from lakehouse.events.control_emit import NoopControlEmitter

    return _on(store, lambda client: proj_ep.create_project(settings=settings, token=token, client=client, announcer=unfiled(NoopControlEmitter()), body=body))


def test_require_project_exists_refuses_an_unknown_tenant(tmp_path: Path) -> None:
    """The guard that REPLACED the bootstrap exception: an unknown project is a 404 (spec code 4,
    problem body) whose detail names the fix — POST /management/v1/projects. Existence comes from the registry,
    identically for every caller, which is why it runs before authz."""
    settings = _fga_settings(control_root=f"file://{tmp_path}")
    with pytest.raises(TableNotFoundError) as exc:
        asyncio.run(hierarchy.require_project_exists(settings, "ghost"))
    assert "POST /management/v1/projects" in str(exc.value)
    # …and an existing record passes untouched.
    seeding.put_project(f"file://{tmp_path}", StorageOptions(), {"id": "acme", "created_at": "t", "created_by": "x"})
    asyncio.run(hierarchy.require_project_exists(settings, "acme"))


def test_estate_admin_creates_a_project_and_the_project_then_governs_itself(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """Decision 1 end to end, on the real model: the estate admin mints the TENANT at POST /management/v1/projects (the
    estate door, ``can_create_project`` on the root), its record and her ``project#admin`` tuple in one operation, and every
    warehouse after that passes on the project's OWN rung: with her estate standing withdrawn she still creates them, and a
    member of the project, the rung below its admin, is refused."""
    from catalog.api.v1.endpoints import projects as proj_ep
    from catalog.schemas import CreateWarehouseRequest

    estate_admin = ("user:dex~alice", "admin", _ROOT_OBJECT)
    openfga_store.write(estate_admin, ("user:dex~bob", "owner", _ROOT_OBJECT))
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: None)
    settings = _governed_settings(tmp_path)

    with pytest.raises(PermissionDeniedError):
        _create_project_as(settings, _caller("dex~bob"), proj_ep.CreateProjectRequest(id="acme"), openfga_store)
    minted = _create_project_as(settings, _caller("dex~alice"), proj_ep.CreateProjectRequest(id="acme"), openfga_store)
    assert ("user:dex~alice", "admin", "project:acme") in openfga_store.tuples()
    assert minted.admins == ["dex~alice"]  # existence and permission born together, and the response shows it

    openfga_store.delete(estate_admin)
    openfga_store.write(("user:dex~carol", "member", "project:acme"))
    for warehouse_id in ("acme-wh", "acme-two"):
        assert _create_as(settings, "dex~alice", CreateWarehouseRequest(id=warehouse_id, project="acme"), openfga_store).id == warehouse_id
        assert ("user:dex~alice", "owner", f"warehouse:{warehouse_id}") in openfga_store.tuples()
    with pytest.raises(PermissionDeniedError):
        _create_as(settings, "dex~carol", CreateWarehouseRequest(id="acme-carol", project="acme"), openfga_store)
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-carol") is None


def test_a_machine_that_mints_a_project_names_its_person_admin_and_holds_nothing(tmp_path: Any, openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """LH-433: a machine never becomes a tenant's admin. A provisioning identity (ADR 0143's operator) that
    creates a project must name the person who administers it; it is refused 400 before anything is written
    when it names nobody or a service account's subject, and when it names a person only that person is seeded."""
    from catalog.api.v1.endpoints import projects as proj_ep
    from lakehouse.governed.machine_identity import ServicePrincipal

    provisioner = fga_ids.user_object("kubernetes~system:serviceaccount:ops:provisioner")
    openfga_store.write((provisioner, "admin", _ROOT_OBJECT))
    settings = _governed_settings(tmp_path)
    machine = ServicePrincipal.for_account("system:serviceaccount:ops:provisioner")

    # A service account's subject, the provisioner's own or another release's, is a machine whatever its namespace.
    for unnamed in (None, "kubernetes~system:serviceaccount:rask:rask-sa-web", "kubernetes~system:serviceaccount:ops:provisioner"):
        with pytest.raises(InvalidInputError):
            _create_project_as(settings, machine, proj_ep.CreateProjectRequest(id="acme", admin=unnamed), openfga_store)
        assert proj_svc.list_projects(settings.registry_root, StorageOptions()) == [], f"a refused create ({unnamed!r}) wrote the tenant anyway"

    minted = _create_project_as(settings, machine, proj_ep.CreateProjectRequest(id="acme", admin="dex~alice"), openfga_store)

    assert minted.admins == ["dex~alice"]
    assert {t for t in openfga_store.tuples() if t[2] == "project:acme"} == {("user:dex~alice", "admin", "project:acme")}, (
        "the machine holds a tuple on the tenant it minted"
    )


def test_an_ordinary_user_cannot_mint_a_tenant_through_any_door(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    # Fail-closed at BOTH doors, before ANY side effect: the project door 403s a non-estate-admin, and
    # the warehouse door 404s the unknown tenant (there is no side door left that would mint it). No
    # bucket, no registry record, no tuple either way.
    from catalog.api.v1.endpoints import projects as proj_ep
    from catalog.schemas import CreateWarehouseRequest

    openfga_store.write(("user:dex~alice", "admin", _ROOT_OBJECT))
    provisioned: list[str] = []
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: provisioned.append(bucket))
    settings = _governed_settings(tmp_path)

    with pytest.raises(PermissionDeniedError):
        _create_project_as(settings, _caller("dex~mallory"), proj_ep.CreateProjectRequest(id="evil"), openfga_store)
    with pytest.raises(TableNotFoundError):
        _create_as(settings, "dex~mallory", CreateWarehouseRequest(id="evil-wh", project="evil"), openfga_store)
    assert provisioned == []
    assert proj_svc.list_projects(settings.registry_root, StorageOptions()) == []
    assert warehouses.list_warehouses(settings.registry_root, StorageOptions()) == []
    assert openfga_store.tuples() == {("user:dex~alice", "admin", _ROOT_OBJECT)}


# --------------------------------------------------------------------------- #
# routing isolation guards (audit F3/F4)
# --------------------------------------------------------------------------- #


def _bare_request() -> Any:
    return SimpleNamespace(state=SimpleNamespace())


def test_a_first_segment_no_record_names_is_served_from_the_estate_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # A name under no warehouse is read in the estate's own catalog, where it is absent: the caller is answered as for
    # any absent name, and learns nothing about which warehouses exist.
    monkeypatch.setattr(warehouses, "get_warehouse", lambda *_a, **_k: None)
    assert asyncio.run(dependencies._resolve_warehouse_root(_bare_request(), _fga_settings(), "wh-a")) is None


@pytest.mark.parametrize(
    ("failure", "refusal"),
    [
        pytest.param(
            OSError("AWS Error NETWORK_CONNECTION during GetObject operation: curlCode: 28, Timeout was reached"), ServiceUnavailableError, id="outage"
        ),
        pytest.param(ValueError("Expecting value: line 1 column 1 (char 0)"), InternalError, id="unreadable-record"),
    ],
)
def test_a_warehouse_record_that_cannot_be_read_is_never_read_as_no_warehouse(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, refusal: type[Exception]
) -> None:
    """Read as "no such warehouse", either failure would serve a tenant's name from the estate's root. A store that did
    not answer is the retryable 503; a record that is there and cannot be read is a 500, which no retry mends."""

    def boom(*_a: object, **_k: object) -> dict[str, str] | None:
        raise failure

    monkeypatch.setattr(warehouses, "get_warehouse", boom)
    with pytest.raises(refusal):
        asyncio.run(dependencies._resolve_warehouse_root(_bare_request(), _fga_settings(), "wh-a"))


# --------------------------------------------------------------------------- #
# reserved buckets: EVERY platform-owned bucket, regardless of zoning
# --------------------------------------------------------------------------- #


def test_reserved_bucket_set_folds_in_the_control_and_multibase_buckets() -> None:
    # The set must cover the shared root, the registry/control bucket and every approved multi-base data
    # bucket, plus the named medallion zone buckets. A warehouse over any of them would hand one project
    # governance of platform (or every tenant's) data there.
    s = Settings.model_validate(
        {
            "root": "s3://lance-catalog",
            "control_root": "s3://lance-control/registry",
            "multibase_data_bases": "s3://lance-data-1, s3://lance-data-2/prefix",
            "reserved_buckets": "lance-source",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )
    assert s.reserved_bucket_set == frozenset({"lance-catalog", "lance-control", "lance-data-1", "lance-data-2", "lance-source"})


def test_reserved_bucket_set_defaults_collapse_to_the_root_bucket() -> None:
    # Defaults: the control root is the catalog root and multibase is off, so nothing extra is reserved,
    # byte-identical to the single-bucket posture. file:// roots contribute no bucket at all.
    s = Settings.model_validate({"s3_access_key_id": "x", "s3_secret_access_key": "x"})
    assert s.reserved_bucket_set == frozenset({"lance-catalog"})
    local = Settings.model_validate({"root": "file:///tmp/cat", "s3_access_key_id": "x", "s3_secret_access_key": "x"})
    assert local.reserved_bucket_set == frozenset()


def test_a_warehouse_record_the_store_does_not_serve_is_an_outage_never_a_record_to_skip() -> None:
    """A record the store did not answer says nothing about the record, so the reader raises the outage (503, code 17),
    tolerant listing or strict, where it skips one that is there and cannot be read as one ([[LH-572]]): skipped, it
    would read as a corrupt record no retry mends. Over a real S3 server, whose GET of the record answers 503."""
    server = ThreadedMotoServer(port=0)
    server.start()
    try:
        host, port = server.get_host_and_port()
        moto = f"http://{host}:{port}"
        bucket = f"reg-{uuid4().hex[:8]}"
        boto3.client("s3", endpoint_url=moto, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=bucket)
        with counted(moto) as store:
            so = lance_storage_options(store.url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")
            seeding.put_warehouse(f"s3://{bucket}", so, {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme", "created_at": "t"})
            with (
                store.answering_unavailable(lambda request: request.method == "GET" and request.path.endswith("/_warehouses/wh-a.json")),
                pytest.raises(ServiceUnavailableError, match="did not answer"),
            ):
                warehouses.list_warehouses(f"s3://{bucket}", so)
    finally:
        server.stop()
