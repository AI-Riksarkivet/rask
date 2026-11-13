"""The catalog converges the projects, warehouses and service grants git declares ([[XC-213]], ADR 0206).

The declaration is the file the chart renders from its values and the catalog mounts. A tick creates what it names and
corrects what drifted, through the functions the doors call, so an install from git holds its tenants with no call to
any door. Driven on the deployed shape: the real app with authorization on (a loopback Dex, a real OpenFGA store of the
test's own) over an S3 store, the tick delivered as the sidecar delivers it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from moto.server import ThreadedMotoServer

from catalog.api.declaration_converge import mount_declaration_converge
from catalog.services import warehouses
from lakehouse.governed import fga
from lakehouse.governed.audit import AUDIT_LOGGER, AuditOutcome


TICK = "catalog-declaration-converge-cron"
ESTATE_BUCKET = "estate"
CONNECTOR, IDP = "local", "dex"
#: A pipeline's service account: the kind of principal a declaration grants, never a person.
PIPELINE = "kubernetes~system:serviceaccount:default:pipeline"
PROBLEM = "application/problem+json"


class Estate:
    """The catalog as deployed with a declaration mounted, and what a test reads it back through."""

    def __init__(self, client: TestClient, store: Any, dex: Any, declaration: Path) -> None:  # noqa: ANN401 — root conftest's OpenFgaStore and DexIssuer
        self.client = client
        self.store = store
        self._dex = dex
        self._declaration = declaration

    def declare(self, **declared: list[dict[str, Any]]) -> None:
        """Replace the declaration, as a reconcile of the chart's ConfigMap does."""
        self._declaration.write_text(json.dumps(declared))

    def tick(self) -> dict[str, Any]:
        """One delivery of the cron binding; the tick's own answer."""
        answer = self.client.post(f"/{TICK}")
        assert answer.status_code == 200, answer.text
        return answer.json()

    def bearer(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._dex.mint(user_id, connector=CONNECTOR, audience='lance-catalog')}"}

    def record(self, warehouse_id: str) -> dict[str, str] | None:
        settings = self.client.app.state.settings
        return warehouses.get_warehouse(settings.registry_root, settings.storage_options(), warehouse_id)


@pytest.fixture(scope="module")
def store_url() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@contextmanager
def _catalog(store_url: str, declaration: Path, monkeypatch: pytest.MonkeyPatch, governed: dict[str, str]) -> Generator[TestClient]:
    """The catalog over an emptied S3 store, ``declaration`` mounted (empty) and its tick served, under ``governed``'s auth settings."""
    s3 = boto3.resource("s3", endpoint_url=store_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    for bucket in s3.buckets.all():
        bucket.objects.all().delete()
        bucket.delete()
    s3.create_bucket(Bucket=ESTATE_BUCKET)
    declaration.write_text("{}")
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{ESTATE_BUCKET}",
        "LANCE_S3_ENDPOINT": store_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_DECLARATION_FILE": str(declaration),
        **governed,
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    mounted = len(app.router.routes)
    mount_declaration_converge(app, TICK)
    with TestClient(app) as client:
        yield client
    del app.router.routes[mounted:]
    get_settings.cache_clear()


@pytest.fixture
def estate(store_url: str, openfga_store: Any, dex_issuer: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Estate]:  # noqa: ANN401 — root conftest's fixtures
    """The catalog as deployed: every caller authenticated, authorization on a real OpenFGA store of the test's own."""
    governed = {
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_CONNECTORS": json.dumps({CONNECTOR: IDP}),
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": openfga_store.url,
        "RASK_FGA_STORE_ID": openfga_store.store_id,
        "RASK_FGA_MODEL_ID": openfga_store.model_id,
    }
    declaration = tmp_path / "declaration.json"
    with _catalog(store_url, declaration, monkeypatch, governed) as client:
        yield Estate(client, openfga_store, dex_issuer, declaration)


@pytest.fixture
def open_estate(store_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Estate]:
    """The catalog on a stack that authenticates nobody: no caller, no authorization store."""
    declaration = tmp_path / "declaration.json"
    with _catalog(store_url, declaration, monkeypatch, {"RASK_OIDC_ENABLED": "false", "RASK_FGA_ENABLED": "false"}) as client:
        yield Estate(client, None, None, declaration)


def _project(project_id: str, *, admin: str = "alice") -> dict[str, Any]:
    return {"id": project_id, "admins": [f"{IDP}~{admin}"]}


def _warehouse(warehouse_id: str, project: str, **spec: Any) -> dict[str, Any]:  # noqa: ANN401 — a declaration's own fields
    return {"id": warehouse_id, "project": project, "bucket": f"{warehouse_id}-bucket", **spec}


def _grant(obj: str, relation: str) -> dict[str, str]:
    return {"object": obj, "relation": relation, "principal": PIPELINE}


def test_a_tick_makes_every_declared_project_warehouse_and_service_grant_with_no_door_called(estate: Estate) -> None:
    estate.declare(
        projects=[_project("acme"), _project("research", admin="bob")],
        warehouses=[_warehouse("acme", "acme"), _warehouse("lab", "research")],
        grants=[_grant("warehouse:acme", "writer")],
    )

    estate.tick()

    alice, bob = estate.bearer("alice"), estate.bearer("bob")
    projects = {project: estate.client.get(f"/management/v1/projects/{project}", headers=who).json() for project, who in (("acme", alice), ("research", bob))}
    listed = {who: sorted(estate.client.get("/v1/namespace/$/list", headers=headers).json()["namespaces"]) for who, headers in (("alice", alice), ("bob", bob))}
    assert {project: (answer["project"], answer["managed_by"]) for project, answer in projects.items()} == {
        "acme": ("acme", "git"),
        "research": ("research", "git"),
    }
    assert listed == {"alice": ["acme"], "bob": ["lab"]}
    assert (fga.user_object(PIPELINE), "writer", "warehouse:acme") in estate.store.tuples()


def _declared_acme(estate: Estate) -> None:
    """An estate whose declaration names project ``acme`` and its warehouse ``acme``, converged."""
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("acme", "acme")])
    estate.tick()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("POST", "/management/v1/warehouses/acme/deactivate", None, id="warehouse-status"),
        pytest.param("POST", "/management/v1/warehouses/acme/protection", {"protected": True}, id="warehouse-protection"),
        pytest.param("POST", "/management/v1/warehouses", {"id": "acme", "project": "acme", "bucket": "acme-bucket"}, id="warehouse-re-post"),
        pytest.param("DELETE", "/management/v1/warehouses/acme", None, id="warehouse-delete"),
        pytest.param("POST", "/management/v1/projects/acme/protection", {"protected": True}, id="project-protection"),
        pytest.param("DELETE", "/management/v1/projects/acme", None, id="project-delete"),
    ],
)
def test_a_door_refuses_to_change_what_git_declares(
    estate: Estate, caplog: pytest.LogCaptureFixture, method: str, path: str, body: dict[str, Any] | None
) -> None:
    """Each door that changes what a declaration owns, asked by the project's own admin, who could otherwise make the change."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    _declared_acme(estate)
    before = (estate.record("acme"), estate.client.get("/management/v1/projects/acme", headers=estate.bearer("alice")).json())

    refused = estate.client.request(method, path, json=body, headers=estate.bearer("alice"))

    assert (refused.status_code, refused.headers["content-type"], refused.json()["code"]) == (403, PROBLEM, 15), refused.text
    assert "declared in git" in refused.json()["detail"]
    assert (estate.record("acme"), estate.client.get("/management/v1/projects/acme", headers=estate.bearer("alice")).json()) == before
    denied = [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__.get("audit.reason") == "managed_by_git"]
    assert [(d["audit.outcome"], d["audit.subject"], d["audit.resource"].partition(":")[2]) for d in denied] == [(AuditOutcome.DENY, f"{IDP}~alice", "acme")]


def test_a_stack_that_authenticates_nobody_still_refuses_a_re_post_of_a_declared_warehouse(open_estate: Estate) -> None:
    _declared_acme(open_estate)
    before = open_estate.record("acme")

    refused = open_estate.client.post("/management/v1/warehouses", json={"id": "acme", "project": "acme", "bucket": "acme-bucket", "protected": True})

    assert (refused.status_code, refused.json()["code"], open_estate.record("acme")) == (403, 15, before), refused.text


def test_a_warehouse_whose_establish_failed_is_completed_by_the_next_tick_and_then_left_alone(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure is the warehouse's row in its own catalog, written after its record: a tick that took the record
    for the whole warehouse would leave it without a row for good, and locked against the re-POST that mends it."""
    row, attempts = warehouses.ensure_catalog_row, []

    def failing_once(ns: Any, warehouse_id: str) -> None:  # noqa: ANN401 — the namespace connection the door hands it
        attempts.append(warehouse_id)
        if attempts.count("acme") == 1:
            raise OSError("the store did not answer")
        row(ns, warehouse_id)

    monkeypatch.setattr(warehouses, "ensure_catalog_row", failing_once)
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("acme", "acme")])

    ticks = [estate.tick() for _ in range(3)]

    made = estate.client.post("/v1/namespace/acme$sales/create", json={}, headers=estate.bearer("alice"))
    assert [(tick["converged"], tick["failed"]) for tick in ticks] == [(["project:acme"], ["warehouse:acme"]), (["warehouse:acme"], []), ([], [])]
    assert (made.status_code, (estate.record("acme") or {}).get("managed_by")) == (200, "git"), made.text


def test_a_warehouse_git_declares_in_another_project_than_it_stands_in_is_left_as_it_is(estate: Estate) -> None:
    projects = [_project("acme"), _project("research", admin="bob")]
    estate.declare(projects=projects, warehouses=[_warehouse("shared", "acme")])
    estate.tick()
    before = estate.record("shared")
    estate.declare(projects=projects, warehouses=[_warehouse("shared", "research", active=False, protected=True)])

    answer = estate.tick()

    assert (answer["converged"], answer["failed"], estate.record("shared")) == ([], ["warehouse:shared"], before)


def test_a_paged_root_listing_shows_every_warehouse_the_caller_sees(estate: Estate) -> None:
    """One warehouse sorts before the estate's own id and one after it: the cursor walks them in one order."""
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("aaa", "acme"), _warehouse("zzz", "acme")])
    estate.tick()
    estate.store.write((fga.user_object(f"{IDP}~alice"), "reader", "warehouse:lance_catalog"))
    alice, seen, token = estate.bearer("alice"), [], ""

    while token is not None:
        page = estate.client.get("/v1/namespace/$/list", params={"limit": 1, **({"page_token": token} if token else {})}, headers=alice).json()
        seen += page["namespaces"]
        token = page.get("page_token")

    assert seen == ["aaa", "lance_catalog", "zzz"]


def test_a_deactivated_warehouse_is_told_only_to_a_caller_its_door_would_admit(estate: Estate) -> None:
    """Bob holds nothing on the warehouse: he is answered for a namespace that stands in it as for a name in no
    warehouse at all. Alice administers it, and is told at a door that opens the namespace and at one that does not."""
    _declared_acme(estate)
    assert estate.client.post("/v1/namespace/acme$sales/create", json={}, headers=estate.bearer("alice")).status_code == 200
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("acme", "acme", active=False)])
    estate.tick()
    describe, managed_access = "/v1/namespace/{name}/describe", "/management/v1/namespace/{name}/managed-access/describe"

    def answered(who: str, name: str, door: str) -> tuple[int, int, str]:
        answer = estate.client.post(door.format(name=name), json={}, headers=estate.bearer(who)).json()
        return answer["status"], answer["code"], answer["detail"].replace(name, "<name>")

    told = {door: answered("alice", "acme$sales", door) for door in (describe, managed_access)}
    assert answered("bob", "acme$sales", describe) == answered("bob", "nowhere$sales", describe)
    assert {door: (status, code, "deactivated" in detail) for door, (status, code, detail) in told.items()} == {
        describe: (403, 15, True),
        managed_access: (403, 15, True),
    }


def test_a_tick_on_an_estate_as_declared_changes_nothing(estate: Estate) -> None:
    _declared_acme(estate)

    answer = estate.tick()

    assert (answer["converged"], answer["grants_written"], answer["failed"]) == ([], 0, [])


@pytest.mark.parametrize(
    "grant",
    [
        pytest.param({"object": "warehouse:../acme", "relation": "writer", "principal": PIPELINE}, id="object-id-no-control-id"),
        pytest.param({"object": "warehouse:acme", "relation": "writer", "principal": f"{IDP}~alice"}, id="a-person"),
    ],
)
def test_a_declaration_granting_what_it_may_not_converges_nothing(estate: Estate, grant: dict[str, str]) -> None:
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("acme", "acme")], grants=[grant])

    answer = estate.tick()

    assert (answer["converged"], answer["failed"], estate.record("acme")) == ([], ["declaration"], None)


def test_a_tick_corrects_a_declared_warehouse_that_drifted(estate: Estate) -> None:
    """The drift is planted on the record itself, as nothing but a write past the doors can make it."""
    _declared_acme(estate)
    settings = estate.client.app.state.settings
    warehouses.set_warehouse_status(settings.registry_root, settings.storage_options(), "acme", "deactivated")

    answer = estate.tick()

    assert (answer["converged"], (estate.record("acme") or {}).get("status")) == (["warehouse:acme"], "active")


def test_a_grant_removed_from_git_is_revoked_and_one_made_at_a_door_stands(estate: Estate) -> None:
    """The catalog revokes only a tuple it wrote from the declaration: the reader grant here was made at the warehouse's
    access door before git ever declared it, so removing it from git takes nothing."""
    _declared_acme(estate)
    at_the_door = estate.client.post(
        "/management/v1/warehouse/acme/access/grant", json={"user": PIPELINE, "relation": "reader"}, headers=estate.bearer("alice")
    )
    assert at_the_door.status_code == 200, at_the_door.text
    estate.declare(
        projects=[_project("acme")], warehouses=[_warehouse("acme", "acme")], grants=[_grant("warehouse:acme", "reader"), _grant("warehouse:acme", "writer")]
    )
    estate.tick()
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("acme", "acme")])

    estate.tick()

    held = {relation for user, relation, obj in estate.store.tuples() if user == fga.user_object(PIPELINE) and obj == "warehouse:acme"}
    assert held == {"reader"}


def _released_acme(estate: Estate) -> dict[str, str]:
    """An estate whose project ``acme`` and warehouse ``acme`` were declared, hold a table, and are declared no more; alice's headers."""
    alice = estate.bearer("alice")
    _declared_acme(estate)
    assert estate.client.post("/v1/namespace/acme$sales/create", json={}, headers=alice).status_code == 200
    assert estate.client.post("/v1/table/acme$sales$t/declare", json={}, headers=alice).status_code == 200
    estate.declare()
    return alice


def test_a_project_removed_from_git_is_released_and_keeps_its_data(estate: Estate) -> None:
    alice = _released_acme(estate)

    answer = estate.tick()

    project = estate.client.get("/management/v1/projects/acme", headers=alice).json()
    assert (sorted(answer["released"]), answer["grants_revoked"], answer["failed"]) == (["project:acme", "warehouse:acme"], 0, [])
    assert (fga.user_object(f"{IDP}~alice"), "admin", "project:acme") in estate.store.tuples()
    assert (project["managed_by"], (estate.record("acme") or {}).get("managed_by")) == ("self", None)
    assert estate.client.get("/v1/namespace/acme$sales/table/list", headers=alice).json()["tables"] == ["t"]


def test_a_released_warehouse_is_changed_at_its_doors_again(estate: Estate) -> None:
    alice = _released_acme(estate)
    estate.tick()

    deactivated = estate.client.post("/management/v1/warehouses/acme/deactivate", headers=alice)

    assert (deactivated.status_code, deactivated.json().get("status")) == (200, "deactivated"), deactivated.text
