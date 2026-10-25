"""Criterion 2, the catalog correct for Lance Namespace and for authorization, on the shared tenant (XC-092).

The catalog serves the Lance Namespace spec (`lance_docs/ns_catalog/spec.yaml`, 54 operations) behind rask's
governance. This module holds both halves on the deployed catalog, through the clients an outside user holds.

1. Each spec operation, sent as a stock client's typed request, round-trips, its answer showing what the request asked
   for, or answers the spec's code 0 (Unsupported) where the catalog declines it: the two batch doors, since the
   catalog judges one table's commit at a time, the two index builds, which the caller's own compute does instead, and
   a materialized view's create and a backfill, which the backend does not implement. The client is pylance's
   (`lance_namespace.connect("rest", ...)`), and for the three operations it does not implement (UpdateFieldMetadata,
   BatchCreateTableVersions, BatchCommitTables) the spec's generated one (`lance_namespace_urllib3_client`). A
   refusal reaches the client as the spec's typed error, a hierarchy refusal included. lancedb and lance-ray open and
   read a table through the same namespace.
2. Who may do what, as a matrix of six principals against every spec door, the vend, grant and revoke doors, a
   classification label's revoke door, and the warehouse and project admin doors. On the matrix namespace the owner,
   the writer and the reader each hold one rung of model.fga's ladder, each rung holding the ones below it, and on the
   matrix label the label's ladder: the owner is its curator, the writer a delegate holding `apply` and `pass_grants`,
   and the reader holds `apply`. The outsider holds none, the tenant-B admin administers
   another tenant, and the service identity is a fleet service account no tuple names. A cell's verdict is read off
   that ladder and the rung each door's gate asks for, never off the model, so a loosened relation or a deleted route
   answers a cell it must not, and that cell goes red. A refused cell answers its code, leaves what the door could
   change as it was, and names nothing the caller did not send.
3. A caller who clears a door's gate still meets the rules rask's hierarchy and the closed rows LH-037, LH-203,
   LH-204, LH-205, LH-208 and LH-209 made: no table outside a namespace and no top-level namespace outside its
   warehouse, a namespace Overwrite only over an empty namespace, a branch only in its own directory, one holder per
   dataset location, no warehouse in another object store, no writer's hand on the platform's `rask.*` provenance, and
   an external blob base only inside an approved one. Each answers its spec code, or the request's success, and leaves
   what it acts on as it was.

A door's rung is what `catalog.api.fga_deps` asks for there, reduced to the ladder by model.fga: the reads ask
`can_get_metadata` or `can_read_data` (reader), the writes `can_write_data` or a create on the parent (writer), and the
lifecycle, ref-plane and grant doors `can_drop`, `can_deregister`, `can_restore`, `can_create_branch`, a tag rung or a
grant rung, each of which ownership carries (owner). The label's revoke door asks `can_revoke_grant`, which only the
label's curator holds (owner): a delegate may hand a label on and may not take one back. The warehouse's and the
project's doors ask for a rung on the warehouse or the project, which no matrix principal holds. Those cells are each
principal's refusal, and the tenant-B admin's own warehouse, which that admin makes, shows its refusals here are not
those of a caller who can do nothing.

THE MATRIX NAMESPACE lives in the shared tenant's warehouse (`byo_scenario.Tenant`), under a name that does not carry
the tenant's project id, so the scenario's tables, and the events criteria 1 and 4 capture by project, stay exactly
what the scenario made. The warehouse's cascade removes it with the tenant.

THE MATRIX LABEL is a classification label of the scene's own. A label is estate vocabulary, which no tenant's cascade
reaches, so the scene removes the label's tuples when the module ends. The estate admin names its curator through the
raw tuple editor, the only door that writes `curator`; the curator grants the delegate through the label's grant door,
and the delegate hands `apply` on through the same door.

WHAT IS NOT CLAIMED, and why. Each operation is driven in the shape its round trip and its gate need, not in every mode
and edge path a request may take: LH-258 (#185) lists the edge paths left, after launch. LH-037's Skip drop finishes the
trailer of a namespace whose native drop died, which no outside caller can stage: an absent id holds no rung, so its drop
answers 403. The audit corpus (LH-231), the governance lifecycle (LH-228) and the in-process tier's per-test OpenFGA
(LH-235) are rows of their own, after launch; this module is the deployed tier, and the e2e-stack lane runs it against a
real OpenFGA on every push.

Run with `bash scripts/e2e_live.sh -m phase1 --require-live` against the deployed estate, `make e2e-phase1` with the
addresses set, or the e2e-stack lane, which runs it on every push.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

import byo_scenario as scenario
import correctness
import lance
import lance.namespace
import lance_namespace as ln
import lance_namespace_urllib3_client.models as wire
import pyarrow as pa
import pytest
import requests
import yaml
from byo_scenario import CATALOG, Identity, Tenant
from lance_namespace_urllib3_client import ApiClient, Configuration
from lance_namespace_urllib3_client.api import TableApi
from lance_namespace_urllib3_client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field


#: The spec the catalog serves: the doors this module drives, and the oracle for each one's request and answer.
SPEC = Path(__file__).resolve().parents[2] / "lance_docs" / "ns_catalog" / "spec.yaml"
WRITER_USER = os.environ.get("LANCE_E2E_WRITER_USER", "carol@example.com")
READER_USER = os.environ.get("LANCE_E2E_READER_USER", "dave@example.com")
OUTSIDER_USER = os.environ.get("LANCE_E2E_OUTSIDER_USER", "erin@example.com")
#: A token a fleet service account (`<release>-sa-lineage`) holds for the catalog's audience, minted by the launcher.
SERVICE_TOKEN_FILE = os.environ.get("LANCE_E2E_SERVICE_TOKEN_FILE", "")
#: The spec's error codes this module reads (spec.yaml, `ErrorResponse.code`).
UNSUPPORTED, NAMESPACE_NOT_EMPTY, TABLE_NOT_FOUND, TABLE_TAG_NOT_FOUND, INVALID_INPUT, PERMISSION_DENIED, INVALID_TABLE_STATE = 0, 3, 4, 8, 13, 15, 19

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.phase1,
    pytest.mark.skipif(not (scenario.LIVE and SERVICE_TOKEN_FILE), reason=f"{scenario.REQUIRES}, and LANCE_E2E_SERVICE_TOKEN_FILE (a service account's token)"),
]


class Rung(IntEnum):
    """The rungs of model.fga's ladder a principal can hold on the matrix namespace, lowest first. ABOVE is any rung no
    matrix principal holds: the warehouse's, the project's, and a view's, which no view exists to grant."""

    NONE = 0
    READER = 1
    WRITER = 2
    OWNER = 3
    ABOVE = 4


class Role(StrEnum):
    """The principals of the matrix."""

    OWNER = "owner"
    WRITER = "writer"
    READER = "reader"
    OUTSIDER = "outsider"
    TENANT_B_ADMIN = "tenant-b-admin"
    SERVICE = "service"


#: The rung each principal holds on the matrix namespace and on everything in it, and the matching rung of the matrix
#: label's ladder, as the scene grants them.
HOLDS: dict[Role, Rung] = {
    Role.OWNER: Rung.OWNER,
    Role.WRITER: Rung.WRITER,
    Role.READER: Rung.READER,
    Role.OUTSIDER: Rung.NONE,
    Role.TENANT_B_ADMIN: Rung.NONE,
    Role.SERVICE: Rung.NONE,
}


class Target(BaseModel):
    """What one call acts on: a table or a namespace, and the name, location, row or staged commit it names."""

    model_config = ConfigDict(frozen=True)

    table: list[str] = Field(default_factory=list)
    namespace: list[str] = Field(default_factory=list)
    name: str = ""
    location: str = ""
    row: int = 0
    staged: dict[str, Any] = Field(default_factory=dict)


class Answer(BaseModel):
    """What a door answered: its value, or the error a caller reads, with its spec code and an HTTP door's status."""

    value: Any = None
    status: int | None = None
    code: int | None = None
    text: str = ""


def _problem_code(text: str) -> int | None:
    """The spec code a problem+json body carries."""
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def attempt(call: Callable[[], Any]) -> Answer:
    """Call a door. pylance's client raises the spec's typed error, the generated client an ApiException carrying the
    problem+json body, and a management door answers HTTP."""
    try:
        value = call()
    except ln.LanceNamespaceError as error:
        return Answer(code=error.code, text=str(error))
    except ApiException as error:
        body = str(error.body or "")
        return Answer(status=error.status, code=_problem_code(body), text=body)
    if not isinstance(value, requests.Response):
        return Answer(value=value)
    if value.ok:
        return Answer(value=value.json() if value.content else None, status=value.status_code)
    return Answer(status=value.status_code, code=_problem_code(value.text), text=value.text)


def eventually(holds: Callable[[], bool], failure: str) -> None:
    """Poll ``holds`` until it is true, failing with ``failure`` once the settle window is spent."""
    deadline = time.monotonic() + scenario.SETTLE_SECONDS
    while not holds():
        assert time.monotonic() < deadline, f"{failure} within {scenario.SETTLE_SECONDS:.0f} s"
        time.sleep(1)


class Stager(lance.namespace.RestNamespace):
    """The stock client, except that a commit pylance stages is kept rather than sent: the version/create request
    pylance built, with the manifest it staged for it. The manifest stays staged until a caller sends the request."""

    staged: list[dict[str, Any]]

    def create_table_version(self, request: dict[str, Any]) -> dict[str, Any]:
        self.staged.append(request)
        raise ln.UnsupportedOperationError("kept, not sent")


class Scene:
    """The matrix's tenant: the matrix namespace and its standing objects, and a caller for each principal.

    Built once per module by the tenant's admin and the namespace's owner. Every read here is the owner's, through the
    stock client and pylance a pipeline uses.
    """

    def __init__(self, tenant: Tenant, callers: dict[Role, Identity], tenant_b: str) -> None:
        self.tenant = tenant
        self.callers = callers
        self.tenant_b = tenant_b
        self.clients = {role: caller.namespace() for role, caller in callers.items()}
        self.owner = self.clients[Role.OWNER]
        self.suffix = uuid.uuid4().hex[:8]
        self.namespace = f"c2-{self.suffix}"
        self.table = [self.namespace, "t"]
        self.schema = [self.namespace, "schema"]
        self.keep = [self.namespace, "keep"]
        self.txn = [self.namespace, "txn"]
        #: A child namespace holding a table, and an empty one: what LH-037's Overwrite refuses and replaces.
        self.full = [self.namespace, "full"]
        self.empty = [self.namespace, "empty"]
        #: A principal no matrix role is, in the idp the people's principals name: whom the grant doors grant to.
        self.grantee = f"{callers[Role.OUTSIDER].principal.split('~', 1)[0]}~c2-grantee-{uuid.uuid4().hex[:8]}"
        #: The matrix label: a classification label of the scene's own, its ladder held by the owner, writer and reader.
        self.label = f"c2-label-{self.suffix}"
        self.location = ""
        #: The uuid the warehouse door minted for the matrix namespace ([[LH-519]]), which every reader's describe answers.
        self.namespace_uuid = ""
        #: A dataset written outside the catalog's doors, relative to the namespace root, which a refused register names.
        self.loose = ""
        #: A table registered at a location written outside the catalog's doors, whose claim a held-location register meets.
        self.held = ""
        #: A commit staged on the matrix table, which a refused version/create names.
        self.staged: dict[str, Any] = {}
        #: The transaction a restore of the matrix's transaction table answered.
        self.transaction = ""

    def build(self) -> None:
        """Make the matrix namespace, grant each rung, and make the standing objects every refused call shares."""
        admin, warehouse = self.tenant.admin, self.tenant.project
        body = {"namespace": self.namespace, "properties": {"rask.tier": "bronze"}}
        created = scenario.ok(admin.http.post(f"{CATALOG}/management/v1/warehouses/{warehouse}/namespaces", json=body, timeout=60))
        self.namespace_uuid = str(created["properties"]["rask.uuid"])
        for role, relation in ((Role.OWNER, "owner"), (Role.WRITER, "writer"), (Role.READER, "reader")):
            grant = {"user": self.callers[role].principal, "relation": relation}
            scenario.ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{self.namespace}/access/grant", json=grant, timeout=60))
            eventually(lambda role=role: self.sees_namespace(role), f"the {role}'s grant was not seen")
        self.make_table("t", range(8))
        self.append(self.table, range(8, 10))
        self.owner.create_table_tag(ln.CreateTableTagRequest(id=self.table, tag="keep", version=1))
        self.owner.create_table_branch(ln.CreateTableBranchRequest(id=self.table, name="keep"))
        self.dataset(self.table).create_scalar_index("id", "BTREE")
        self.location = str(self.owner.describe_table(ln.DescribeTableRequest(id=self.table)).location)
        self.staged = self.stage(self.table)
        self.make_table("schema", range(4))
        self.add_column(self.schema, "keep_alter")
        self.add_column(self.schema, "keep_drop")
        self.make_namespace("keep")
        self.grant(self.keep, "reader")
        self.make_namespace("full")
        self.owner.create_table(ln.CreateTableRequest(id=[*self.full, "t"]), scenario.arrow_ipc(scenario.rows_of(range(3))))
        self.make_namespace("empty")
        for name in ("keep-drop", "keep-deregister", "keep-rename"):
            self.make_table(name)
        self.append(self.make_table("keep-restore"), range(3, 5))
        versions = self.make_table("keep-versions")
        self.append(versions, range(3, 5))
        self.append(versions, range(5, 7))
        self.loose = self.written_outside("loose")
        self.held = self.written_outside("held")
        self.owner.register_table(ln.RegisterTableRequest(id=[self.namespace, "held"], location=self.held))
        self.append(self.make_table("txn"), range(3, 5))
        self.transaction = str(self.owner.restore_table(ln.RestoreTableRequest(id=self.txn, version=1)).transaction_id)
        self.make_label()

    def make_label(self) -> None:
        """Hold the matrix label's ladder: the owner its curator, named through the estate admin's tuple editor; the
        writer a delegate, granted `apply` and `pass_grants` by the curator; and the reader and the grantee `apply`,
        which the delegate hands on."""
        curator = {"user": self.callers[Role.OWNER].principal, "relation": "curator", "object": f"classification:{self.label}"}
        scenario.ok(self.tenant.admin.http.post(f"{CATALOG}/management/v1/access/tuples", json=curator, timeout=60))
        eventually(lambda: self.label_permission(Role.OWNER, "can_grant_pass_grants"), "the owner's curator was not seen")
        for relation in ("apply", "pass_grants"):
            self.label_grant(Role.OWNER, self.callers[Role.WRITER].principal, relation)
        eventually(lambda: self.label_permission(Role.WRITER, "can_grant_apply"), "the writer's delegation was not seen")
        for grantee in (self.callers[Role.READER].principal, self.grantee):
            self.label_grant(Role.WRITER, grantee, "apply")

    def remove_label(self) -> list[str]:
        """Delete every tuple the matrix label holds, as the estate admin; the ones still held after."""
        for held in self.label_tuples():
            self.tenant.admin.http.delete(f"{CATALOG}/management/v1/access/tuples", json=held, timeout=60)
        return [f"{held['user']} {held['relation']}" for held in self.label_tuples()]

    # What a principal is, to a client.

    def properties(self, role: Role) -> dict[str, str]:
        """The connect properties of a stock client acting as ``role``, each request on its own connection
        (`byo_scenario.CLOSE_EACH_REQUEST`)."""
        return {"uri": CATALOG, "headers.Authorization": f"Bearer {self.callers[role].bearer}", "headers.Connection": scenario.CLOSE_EACH_REQUEST}

    def generated(self, role: Role) -> TableApi:
        """The spec's generated client acting as ``role``, for the operations pylance's client does not implement."""
        return TableApi(ApiClient(Configuration(host=CATALOG), header_name="Authorization", header_value=f"Bearer {self.callers[role].bearer}"))

    def sees_namespace(self, role: Role) -> bool:
        """Whether ``role`` may describe the matrix namespace: a grant written to the store is not yet one every check reads."""
        return attempt(lambda: self.clients[role].describe_namespace(ln.DescribeNamespaceRequest(id=[self.namespace]))).code is None

    def http(self, role: Role) -> requests.Session:
        return self.callers[role].http

    def foreign(self, role: Role, *, names_tenant: bool) -> set[str]:
        """What a refusal of ``role`` must not name: where the table's bytes live, the tenant's project and bucket unless
        the door's path names them, the other tenant, and every other principal."""
        tenant = set() if names_tenant else {self.tenant.project}
        return {self.location, self.tenant_b, *tenant, *(caller.principal for other, caller in self.callers.items() if other is not role)}

    # Reads, as the owner.

    def dataset(self, table: list[str]) -> lance.LanceDataset:
        return lance.dataset(namespace_client=self.owner, table_id=table)

    def versions(self, table: list[str]) -> list[int]:
        found: list[int] = []
        token: str | None = None
        while True:
            page = self.owner.list_table_versions(ln.ListTableVersionsRequest(id=table, page_token=token))
            found += [int(version.version) for version in page.versions]
            token = page.page_token
            if not token:
                return sorted(found)

    def latest(self, table: list[str]) -> int:
        return self.versions(table)[-1]

    def manifest(self, table: list[str], version: int) -> str:
        """The manifest path the owner's version listing names for ``version``."""
        listed = self.owner.list_table_versions(ln.ListTableVersionsRequest(id=table))
        return next(str(entry.manifest_path) for entry in listed.versions if entry.version == version)

    def rows(self, table: list[str]) -> int:
        return self.dataset(table).count_rows()

    def ids(self, table: list[str]) -> list[int]:
        return sorted(self.dataset(table).to_table(columns=["id"]).column("id").to_pylist())

    def exists(self, table: list[str]) -> bool:
        return attempt(lambda: self.owner.table_exists(ln.TableExistsRequest(id=table))).code is None

    def tags(self, table: list[str]) -> dict[str, int]:
        return {name: int(tag["version"]) for name, tag in self.dataset(table).tags.list().items()}

    def branches(self, table: list[str]) -> list[str]:
        return sorted(self.dataset(table).branches.list())

    def indices(self, table: list[str]) -> list[str]:
        return sorted(index.name for index in self.dataset(table).describe_indices())

    def columns(self, table: list[str]) -> list[str]:
        return list(self.dataset(table).schema.names)

    def field_metadata(self, table: list[str], field: str) -> dict[str, str]:
        metadata = self.dataset(table).schema.field(field).metadata or {}
        return {key.decode(): value.decode() for key, value in metadata.items()}

    def children(self) -> list[str]:
        return sorted(self.owner.list_namespaces(ln.ListNamespacesRequest(id=[self.namespace])).namespaces)

    def tables(self, *, declared: bool = False) -> list[str]:
        return sorted(self.owner.list_tables(ln.ListTablesRequest(id=[self.namespace], include_declared=declared)).tables)

    def access(self, namespace: list[str]) -> dict[str, list[str]]:
        """Who holds each `can_*` on ``namespace``, as its owner reviews it."""
        body = scenario.ok(self.http(Role.OWNER).post(f"{CATALOG}/management/v1/namespace/{'$'.join(namespace)}/access/list", json={}, timeout=60))
        return {grant["relation"]: sorted(grant["users"]) for grant in body["grants"]}

    def granted(self, namespace: list[str], relation: str) -> bool:
        """Whether the grantee holds ``relation``'s `can_*` on ``namespace``: `can_create_table` for a writer, else `can_get_metadata`."""
        door = "can_create_table" if relation == "writer" else "can_get_metadata"
        return any(self.grantee in user for user in self.access(namespace).get(door, []))

    def label_permission(self, role: Role, permission: str) -> bool:
        """Whether ``role`` holds ``permission`` on the matrix label, as its own permission self-view answers. A refused
        self-view is a holder the store does not show yet."""
        answer = self.http(role).post(f"{CATALOG}/management/v1/classification/{self.label}/access/my-permissions", timeout=60)
        if answer.status_code == 403:
            return False
        return bool(scenario.ok(answer)["permissions"].get(permission))

    def label_tuples(self) -> list[dict[str, Any]]:
        """Every tuple the store holds on the matrix label, as the estate admin reads it, page by page."""
        found: list[dict[str, Any]] = []
        params = {"object": f"classification:{self.label}"}
        while True:
            page = scenario.ok(self.tenant.admin.http.get(f"{CATALOG}/management/v1/access/tuples", params=params, timeout=60))
            found += page["tuples"]
            if not page.get("continuation"):
                return found
            params["continuation"] = page["continuation"]

    def label_holders(self, relation: str) -> list[str]:
        """The users the store holds ``relation`` for on the matrix label."""
        return sorted(held["user"] for held in self.label_tuples() if held["relation"] == relation)

    def as_admin(self, path: str) -> requests.Response:
        return self.tenant.admin.http.get(f"{CATALOG}{path}", timeout=60)

    def warehouses_of(self, project: str) -> list[str]:
        """The ids of ``project``'s warehouses, as the estate admin who made both tenants lists them."""
        body = scenario.ok(self.as_admin("/management/v1/warehouses"))
        records = body if isinstance(body, list) else body.get("warehouses") or body.get("items") or []
        return sorted(str(record.get("id")) for record in records if record.get("project") == project)

    # Makes, as the owner.

    def make_table(self, name: str, ids: range = range(3)) -> list[str]:
        table = [self.namespace, name]
        self.owner.create_table(ln.CreateTableRequest(id=table), scenario.arrow_ipc(scenario.rows_of(ids)))
        return table

    def append(self, table: list[str], ids: range) -> None:
        self.owner.insert_into_table(ln.InsertIntoTableRequest(id=table), scenario.arrow_ipc(scenario.rows_of(ids)))

    def add_column(self, table: list[str], name: str) -> None:
        self.owner.alter_table_add_columns(ln.AlterTableAddColumnsRequest(id=table, new_columns=[wire.AddColumnsEntry(name=name, expression="id")]))

    def make_namespace(self, name: str) -> list[str]:
        namespace = [self.namespace, name]
        self.owner.create_namespace(ln.CreateNamespaceRequest(id=namespace))
        return namespace

    def grant(self, namespace: list[str], relation: str) -> None:
        body = {"user": self.grantee, "relation": relation}
        scenario.ok(self.http(Role.OWNER).post(f"{CATALOG}/management/v1/namespace/{'$'.join(namespace)}/access/grant", json=body, timeout=60))

    def label_grant(self, role: Role, grantee: str, relation: str) -> None:
        """``role`` grants ``grantee`` ``relation`` on the matrix label through the label's grant door."""
        body = {"user": grantee, "relation": relation}
        scenario.ok(self.http(role).post(f"{CATALOG}/management/v1/classification/{self.label}/access/grant", json=body, timeout=60))

    def written_outside(self, name: str) -> str:
        """A dataset a pipeline wrote outside the catalog's doors, beside the matrix's tables in their bucket; its location
        relative to the namespace root, as a register names it. A table the catalog made is not registrable again: its
        directory is the catalog's layout ([[LH-520]])."""
        bucket = self.location.removeprefix("s3://").split("/", 1)[0]
        relative = f"{self.namespace}-outside/{name}"
        rows = scenario.rows_of(range(3))
        lance.write_dataset(
            rows, f"s3://{bucket}/{relative}", storage_options=correctness.store_options(), data_storage_version="2.2", enable_stable_row_ids=True
        )
        return relative

    def stage(self, table: list[str]) -> dict[str, Any]:
        """A two-row append pylance staged as the owner, and the version/create request it built for it."""
        stager = Stager(**self.properties(Role.OWNER))
        stager.staged = []
        # pylance wraps the client's refusal to send in an OSError, the commit's outcome unknown to it.
        with contextlib.suppress(OSError):
            lance.write_dataset(scenario.rows_of(range(4000, 4002)), namespace_client=stager, table_id=table, mode="append")
        assert len(stager.staged) == 1, f"pylance staged {len(stager.staged)} commits for one append"
        return stager.staged[0]

    def query(self) -> ln.QueryTableRequest:
        """The matrix table's first three ids, as a scan: a query naming no vector."""
        return ln.QueryTableRequest(
            id=self.table,
            k=10,
            vector=wire.QueryTableRequestVector(single_vector=[]),
            filter="id < 3",
            columns=wire.QueryTableRequestColumns(column_names=["id"]),
        )


def _slug(role: Role) -> str:
    """``role`` as a column name takes it."""
    return str(role).replace("-", "_")


def _queried_ids(data: bytes) -> list[int]:
    """The ids an Arrow IPC answer of a query carries."""
    return sorted(pa.ipc.open_file(pa.BufferReader(data)).read_all().column("id").to_pylist())


def _all_tables(client: ln.LanceNamespace) -> list[str]:
    """Every table ListAllTables shows the caller, page by page."""
    found: list[str] = []
    token: str | None = None
    while True:
        page = client.list_all_tables(ln.ListTablesRequest(page_token=token))
        found += list(page.tables)
        token = page.page_token
        if not token:
            return found


def _tag_code(scene: Scene, tag: str) -> int | None:
    """The spec code reading ``tag`` answers: None while it names a version."""
    return attempt(lambda: scene.owner.get_table_tag_version(ln.GetTableTagVersionRequest(id=scene.table, tag=tag))).code


def _nothing(scene: Scene, target: Target) -> None:
    """A read changes nothing a later read could tell apart."""


def _unshown(scene: Scene, role: Role, target: Target, value: object, before: object) -> tuple[object, object]:
    raise AssertionError("this door holds no answered call to what it asked for")


def _at_table(scene: Scene, role: Role, allowed: bool) -> Target:
    return Target(table=scene.table)


def _at_schema(scene: Scene, role: Role, allowed: bool) -> Target:
    return Target(table=scene.schema)


def _at_namespace(scene: Scene, role: Role, allowed: bool) -> Target:
    return Target(namespace=[scene.namespace])


def _version_and_rows(scene: Scene, target: Target) -> tuple[int, int]:
    return scene.latest(target.table), scene.rows(target.table)


class Door(BaseModel):
    """One door and the gate in front of it.

    ``rung`` is the least rung of the ladder that clears the gate. Past the gate, a ``declined`` door answers the spec's
    code 0. A refused caller reads ``refused``: the HTTP status of a door a client reaches over plain HTTP (pylance's
    typed error carries none) and the spec code. ``names_tenant`` marks a door whose path names the tenant's project and
    warehouse, which its refusal may therefore repeat.

    ``aim`` picks what a call acts on: a fresh object where an allowed call would consume one, else the scene's standing
    one, which every refused call shares. ``send`` makes the call as a principal. ``watch`` reads, as the owner, what the
    call could change. ``shows`` holds an answered call to what it asked for, as ``(observed, expected)``, given its
    value and what ``watch`` read before it.
    """

    model_config = ConfigDict(frozen=True)

    operation: str
    rung: Rung
    declined: bool = False
    refused: tuple[int | None, int] = (None, PERMISSION_DENIED)
    names_tenant: bool = False
    aim: Callable[[Scene, Role, bool], Target] = _at_table
    send: Callable[[Scene, Role, Target], Any]
    watch: Callable[[Scene, Target], object] = _nothing
    shows: Callable[[Scene, Role, Target, Any, Any], tuple[object, object]] = _unshown


def _fresh_table(prefix: str, standing: str) -> Callable[[Scene, Role, bool], Target]:
    """A table of its own for an allowed call, which consumes it; the standing ``standing`` for a refused one."""

    def aim(scene: Scene, role: Role, allowed: bool) -> Target:
        return Target(table=scene.make_table(f"{prefix}-{role}") if allowed else [scene.namespace, standing], name=f"{prefix}-{role}-renamed")

    return aim


def _fresh_child(prefix: str, *, granted: str = "") -> Callable[[Scene, Role, bool], Target]:
    """A child namespace of its own for an allowed call, the grantee holding ``granted`` there; the standing `keep`
    child, where the grantee holds `reader`, for a refused one. A namespace and a table share the `dir` backend's object
    ids, so each door's namespaces carry a prefix no table's name does."""

    def aim(scene: Scene, role: Role, allowed: bool) -> Target:
        if not allowed:
            return Target(namespace=scene.keep)
        child = scene.make_namespace(f"{prefix}-{role}")
        if granted:
            scene.grant(child, granted)
        return Target(namespace=child)

    return aim


def _fresh_column(prefix: str) -> Callable[[Scene, Role, bool], Target]:
    """A column of its own on the schema table for an allowed call; the standing `keep_<prefix>` for a refused one."""

    def aim(scene: Scene, role: Role, allowed: bool) -> Target:
        if not allowed:
            return Target(table=scene.schema, name=f"keep_{prefix}")
        name = f"{prefix}_{_slug(role)}"
        scene.add_column(scene.schema, name)
        return Target(table=scene.schema, name=name)

    return aim


def _fresh_tag(prefix: str) -> Callable[[Scene, Role, bool], Target]:
    """A tag of its own at version 1 for an allowed call; the standing `keep` tag for a refused one."""

    def aim(scene: Scene, role: Role, allowed: bool) -> Target:
        if not allowed:
            return Target(table=scene.table, name="keep")
        scene.owner.create_table_tag(ln.CreateTableTagRequest(id=scene.table, tag=f"{prefix}-{role}", version=1))
        return Target(table=scene.table, name=f"{prefix}-{role}")

    return aim


def _fresh_branch(scene: Scene, role: Role, allowed: bool) -> Target:
    """A branch of its own for an allowed delete; the standing `keep` branch for a refused one."""
    if not allowed:
        return Target(table=scene.table, name="keep")
    scene.owner.create_table_branch(ln.CreateTableBranchRequest(id=scene.table, name=f"gone-{role}"))
    return Target(table=scene.table, name=f"gone-{role}")


def _fresh_registration(scene: Scene, role: Role, allowed: bool) -> Target:
    """A deregistered location of its own for an allowed register; the standing loose one for a refused one."""
    if not allowed:
        return Target(table=[scene.namespace, "registered-refused"], location=scene.loose)
    return Target(table=[scene.namespace, f"registered-{role}"], location=scene.written_outside(f"loose-{role}"))


def _fresh_versions(scene: Scene, role: Role, allowed: bool) -> Target:
    """A table of its own with three versions for an allowed delete of version 2; the standing one for a refused one."""
    if not allowed:
        return Target(table=[scene.namespace, "keep-versions"])
    table = scene.make_table(f"versions-{role}")
    scene.append(table, range(3, 5))
    scene.append(table, range(5, 7))
    return Target(table=table)


def _fresh_index(scene: Scene, role: Role, allowed: bool) -> Target:
    """A table of its own with an `id` index for an allowed drop; the matrix table's index for a refused one."""
    if not allowed:
        return Target(table=scene.table)
    table = scene.make_table(f"index-{role}")
    scene.dataset(table).create_scalar_index("id", "BTREE")
    return Target(table=table)


def _fresh_row(scene: Scene, role: Role, allowed: bool) -> Target:
    """A row of its own for an allowed delete; a base row no cell deletes for a refused one."""
    if not allowed:
        return Target(table=scene.table, row=7)
    row = 3000 + list(Role).index(role)
    scene.append(scene.table, range(row, row + 1))
    return Target(table=scene.table, row=row)


def _fresh_stage(scene: Scene, role: Role, allowed: bool) -> Target:
    """A commit pylance staged for an allowed version/create; the standing staged one for a refused one."""
    return Target(table=scene.table, staged=scene.stage(scene.table) if allowed else scene.staged)


def _vend(tier: str) -> Callable[[Scene, Role, Target], requests.Response]:
    def send(scene: Scene, role: Role, target: Target) -> requests.Response:
        path = f"{CATALOG}/management/v1/table/{'%24'.join(target.table)}/credentials"
        return scene.http(role).post(path, params={"tier": tier}, json={"id": target.table}, timeout=60)

    return send


def _vended(scene: Scene, role: Role, target: Target, value: Any, before: Any) -> tuple[object, object]:
    options = (value.get("credentials") or value).get("storage_options") or {}
    return {"aws_access_key_id", "aws_secret_access_key", "aws_session_token", "endpoint"} <= set(options), True


def _access_door(verb: str, relation: str) -> Callable[[Scene, Role, Target], requests.Response]:
    def send(scene: Scene, role: Role, target: Target) -> requests.Response:
        body = {"user": scene.grantee, "relation": relation}
        return scene.http(role).post(f"{CATALOG}/management/v1/namespace/{'$'.join(target.namespace)}/access/{verb}", json=body, timeout=60)

    return send


def _fresh_label_holder(scene: Scene, role: Role, allowed: bool) -> Target:
    """A grantee of its own, handed `apply` on the matrix label by the delegate, for an allowed revoke; the standing
    grantee, which the delegate handed `apply` at the scene's making, for a refused one."""
    if not allowed:
        return Target(name=scene.grantee)
    holder = f"{scene.grantee.split('~', 1)[0]}~c2-apply-{uuid.uuid4().hex[:8]}"
    scene.label_grant(Role.WRITER, holder, "apply")
    return Target(name=holder)


def _revoke_label_apply(scene: Scene, role: Role, target: Target) -> requests.Response:
    body = {"user": target.name, "relation": "apply"}
    return scene.http(role).post(f"{CATALOG}/management/v1/classification/{scene.label}/access/revoke", json=body, timeout=60)


def _warehouse_namespaces(scene: Scene, target: Target) -> list[str]:
    body = scenario.ok(scene.as_admin(f"/management/v1/warehouses/{scene.tenant.project}/namespaces"))
    return sorted(json.dumps(item, sort_keys=True) for item in body.get("namespaces") or body.get("items") or [])


def _project_warehouses(scene: Scene, target: Target) -> list[str]:
    return scene.warehouses_of(scene.tenant.project)


def _project_members(scene: Scene, target: Target) -> object:
    return scenario.ok(scene.as_admin(f"/management/v1/projects/{scene.tenant.project}/members"))


SPEC_DOORS: tuple[Door, ...] = (
    Door(
        operation="CreateNamespace",
        rung=Rung.WRITER,
        aim=lambda s, role, allowed: Target(namespace=[s.namespace, f"ns-made-{role}"]),
        send=lambda s, role, t: s.clients[role].create_namespace(ln.CreateNamespaceRequest(id=t.namespace)),
        watch=lambda s, t: s.children(),
        shows=lambda s, role, t, value, before: (s.children(), sorted([*before, t.namespace[-1]])),
    ),
    Door(
        operation="ListNamespaces",
        rung=Rung.READER,
        aim=_at_namespace,
        send=lambda s, role, t: s.clients[role].list_namespaces(ln.ListNamespacesRequest(id=t.namespace)),
        shows=lambda s, role, t, value, before: (sorted(value.namespaces), s.children()),
    ),
    Door(
        operation="DescribeNamespace",
        rung=Rung.READER,
        aim=_at_namespace,
        send=lambda s, role, t: s.clients[role].describe_namespace(ln.DescribeNamespaceRequest(id=t.namespace)),
        shows=lambda s, role, t, value, before: (value.properties, {"rask.tier": "bronze", "rask.uuid": s.namespace_uuid}),
    ),
    Door(
        operation="DropNamespace",
        rung=Rung.OWNER,
        aim=_fresh_child("ns-dropped"),
        send=lambda s, role, t: s.clients[role].drop_namespace(ln.DropNamespaceRequest(id=t.namespace)),
        watch=lambda s, t: s.children(),
        shows=lambda s, role, t, value, before: (s.children(), [child for child in before if child != t.namespace[-1]]),
    ),
    Door(
        operation="NamespaceExists",
        rung=Rung.READER,
        aim=_at_namespace,
        send=lambda s, role, t: s.clients[role].namespace_exists(ln.NamespaceExistsRequest(id=t.namespace)),
        shows=lambda s, role, t, value, before: (value, ln.NamespaceExistsResponse()),
    ),
    Door(
        operation="ListTables",
        rung=Rung.READER,
        aim=_at_namespace,
        send=lambda s, role, t: s.clients[role].list_tables(ln.ListTablesRequest(id=t.namespace)),
        # The request sends the spec's default, `include_declared` true (spec.yaml:2356-2367), so it is read against the
        # same listing.
        shows=lambda s, role, t, value, before: (sorted(value.tables), s.tables(declared=True)),
    ),
    # Open to every signed-in caller, and filtered: each sees the tables it may read, so the matrix table is in the
    # listing exactly for the principals that hold the reader rung.
    Door(
        operation="ListAllTables",
        rung=Rung.NONE,
        send=lambda s, role, t: _all_tables(s.clients[role]),
        shows=lambda s, role, t, value, before: ("$".join(t.table) in value, HOLDS[role] >= Rung.READER),
    ),
    Door(
        operation="RegisterTable",
        rung=Rung.WRITER,
        aim=_fresh_registration,
        send=lambda s, role, t: s.clients[role].register_table(ln.RegisterTableRequest(id=t.table, location=t.location)),
        watch=lambda s, t: s.exists(t.table),
        shows=lambda s, role, t, value, before: (s.ids(t.table), [0, 1, 2]),
    ),
    Door(
        operation="DescribeTable",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].describe_table(ln.DescribeTableRequest(id=t.table)),
        shows=lambda s, role, t, value, before: ((value.table, value.location), ("t", s.location)),
    ),
    Door(
        operation="TableExists",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].table_exists(ln.TableExistsRequest(id=t.table)),
        shows=lambda s, role, t, value, before: (value, ln.TableExistsResponse()),
    ),
    Door(
        operation="DropTable",
        rung=Rung.OWNER,
        aim=_fresh_table("dropped", "keep-drop"),
        send=lambda s, role, t: s.clients[role].drop_table(ln.DropTableRequest(id=t.table)),
        watch=lambda s, t: s.exists(t.table),
        shows=lambda s, role, t, value, before: (s.exists(t.table), False),
    ),
    Door(
        operation="DeregisterTable",
        rung=Rung.OWNER,
        aim=_fresh_table("deregistered", "keep-deregister"),
        send=lambda s, role, t: s.clients[role].deregister_table(ln.DeregisterTableRequest(id=t.table)),
        watch=lambda s, t: s.exists(t.table),
        shows=lambda s, role, t, value, before: (s.exists(t.table), False),
    ),
    # Restoring adds a version holding the restored one's rows, so the standing table serves every cell.
    Door(
        operation="RestoreTable",
        rung=Rung.OWNER,
        aim=lambda s, role, allowed: Target(table=[s.namespace, "keep-restore"]),
        send=lambda s, role, t: s.clients[role].restore_table(ln.RestoreTableRequest(id=t.table, version=1)),
        watch=lambda s, t: s.latest(t.table),
        shows=lambda s, role, t, value, before: ((s.latest(t.table), s.ids(t.table)), (before + 1, [0, 1, 2])),
    ),
    Door(
        operation="RenameTable",
        rung=Rung.OWNER,
        aim=_fresh_table("renamed", "keep-rename"),
        send=lambda s, role, t: s.clients[role].rename_table(ln.RenameTableRequest(id=t.table, new_table_name=t.name)),
        watch=lambda s, t: (s.exists(t.table), s.exists([s.namespace, t.name])),
        shows=lambda s, role, t, value, before: ((s.exists(t.table), s.exists([s.namespace, t.name])), (False, True)),
    ),
    Door(
        operation="UpdateTableSchemaMetadata",
        rung=Rung.WRITER,
        send=lambda s, role, t: s.clients[role].update_table_schema_metadata(
            ln.UpdateTableSchemaMetadataRequest(id=t.table, metadata={f"by-{role}": str(role)})
        ),
        watch=lambda s, t: s.latest(t.table),
        shows=lambda s, role, t, value, before: ((value.metadata or {}).get(f"by-{role}"), str(role)),
    ),
    Door(
        operation="ListTableVersions",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].list_table_versions(ln.ListTableVersionsRequest(id=t.table)),
        shows=lambda s, role, t, value, before: (sorted(int(version.version) for version in value.versions), s.versions(t.table)),
    ),
    # pylance's client takes version/create's request as the dict pylance builds for it when it stages a commit, so
    # each principal sends the request a stock writer sends, naming a manifest the stock writer staged.
    Door(
        operation="CreateTableVersion",
        rung=Rung.WRITER,
        aim=_fresh_stage,
        send=lambda s, role, t: s.clients[role].create_table_version(dict(t.staged)),
        watch=lambda s, t: s.latest(t.table),
        shows=lambda s, role, t, value, before: (s.latest(t.table), t.staged["version"]),
    ),
    # pylance's client takes this request as a dict, so the typed request is sent as its own dict.
    Door(
        operation="DescribeTableVersion",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].describe_table_version(ln.DescribeTableVersionRequest(id=t.table, version=1).to_dict()),
        shows=lambda s, role, t, value, before: (
            (value["version"]["version"], value["version"]["manifest_path"]),
            (1, s.manifest(t.table, 1)),
        ),
    ),
    Door(
        operation="BatchDeleteTableVersions",
        rung=Rung.OWNER,
        aim=_fresh_versions,
        send=lambda s, role, t: s.clients[role].batch_delete_table_versions(
            ln.BatchDeleteTableVersionsRequest(id=t.table, ranges=[wire.VersionRange(start_version=2, end_version=3)]).to_dict()
        ),
        watch=lambda s, t: s.versions(t.table),
        shows=lambda s, role, t, value, before: (s.versions(t.table), [1, 3]),
    ),
    # The catalog judges each commit per table at version/create (LH-206), so both batch doors decline once past their
    # gate, which is each named table's rung at its own door.
    Door(
        operation="BatchCreateTableVersions",
        rung=Rung.WRITER,
        declined=True,
        refused=(403, PERMISSION_DENIED),
        send=lambda s, role, t: s.generated(role).batch_create_table_versions(
            ln.BatchCreateTableVersionsRequest(
                entries=[ln.CreateTableVersionEntry(id=t.table, version=s.staged["version"], manifest_path=s.staged["manifest_path"])]
            )
        ),
        watch=lambda s, t: s.latest(t.table),
    ),
    Door(
        operation="BatchCommitTables",
        rung=Rung.OWNER,
        declined=True,
        refused=(403, PERMISSION_DENIED),
        send=lambda s, role, t: s.generated(role).batch_commit_tables(
            ln.BatchCommitTablesRequest(operations=[ln.CommitTableOperation(deregister_table=ln.DeregisterTableRequest(id=t.table))])
        ),
        watch=lambda s, t: s.exists(t.table),
    ),
    Door(
        operation="AlterTableAlterColumns",
        rung=Rung.WRITER,
        aim=_fresh_column("alter"),
        send=lambda s, role, t: s.clients[role].alter_table_alter_columns(
            ln.AlterTableAlterColumnsRequest(id=t.table, alterations=[wire.AlterColumnsEntry(path=t.name, rename=f"{t.name}_renamed")])
        ),
        watch=lambda s, t: s.columns(t.table),
        shows=lambda s, role, t, value, before: (
            (t.name in s.columns(t.table), f"{t.name}_renamed" in s.columns(t.table)),
            (False, True),
        ),
    ),
    Door(
        operation="UpdateFieldMetadata",
        rung=Rung.WRITER,
        refused=(403, PERMISSION_DENIED),
        aim=_at_schema,
        send=lambda s, role, t: s.generated(role).update_field_metadata(
            "$".join(t.table), ln.UpdateFieldMetadataRequest(updates=[wire.UpdateFieldMetadataEntry(path="id", metadata={f"by-{role}": str(role)})])
        ),
        watch=lambda s, t: s.latest(t.table),
        shows=lambda s, role, t, value, before: (s.field_metadata(t.table, "id").get(f"by-{role}"), str(role)),
    ),
    Door(
        operation="AlterTableDropColumns",
        rung=Rung.WRITER,
        aim=_fresh_column("drop"),
        send=lambda s, role, t: s.clients[role].alter_table_drop_columns(ln.AlterTableDropColumnsRequest(id=t.table, columns=[t.name])),
        watch=lambda s, t: s.columns(t.table),
        shows=lambda s, role, t, value, before: (t.name in s.columns(t.table), False),
    ),
    Door(
        operation="GetTableStats",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].get_table_stats(ln.GetTableStatsRequest(id=t.table)),
        shows=lambda s, role, t, value, before: (value.num_rows, s.rows(t.table)),
    ),
    Door(
        operation="InsertIntoTable",
        rung=Rung.WRITER,
        send=lambda s, role, t: s.clients[role].insert_into_table(
            ln.InsertIntoTableRequest(id=t.table),
            scenario.arrow_ipc(scenario.rows_of(range(1000 + 10 * list(Role).index(role), 1002 + 10 * list(Role).index(role)))),
        ),
        watch=_version_and_rows,
        shows=lambda s, role, t, value, before: ((value.num_inserted_rows, s.rows(t.table)), (2, before[1] + 2)),
    ),
    Door(
        operation="MergeInsertIntoTable",
        rung=Rung.WRITER,
        send=lambda s, role, t: s.clients[role].merge_insert_into_table(
            ln.MergeInsertIntoTableRequest(id=t.table, on="id", when_matched_update_all=True, when_not_matched_insert_all=True),
            scenario.arrow_ipc(pa.table({"id": pa.array([0, 2000 + list(Role).index(role)], pa.int64()), "payload": pa.array([f"merged by {role}"] * 2)})),
        ),
        watch=_version_and_rows,
        shows=lambda s, role, t, value, before: ((value.num_updated_rows, value.num_inserted_rows), (1, 1)),
    ),
    Door(
        operation="UpdateTable",
        rung=Rung.WRITER,
        send=lambda s, role, t: s.clients[role].update_table(
            ln.UpdateTableRequest(id=t.table, updates=[["payload", f"'updated by {role}'"]], predicate="id = 1")
        ),
        watch=_version_and_rows,
        shows=lambda s, role, t, value, before: (value.updated_rows, 1),
    ),
    Door(
        operation="DeleteFromTable",
        rung=Rung.WRITER,
        aim=_fresh_row,
        send=lambda s, role, t: s.clients[role].delete_from_table(ln.DeleteFromTableRequest(id=t.table, predicate=f"id = {t.row}")),
        watch=_version_and_rows,
        shows=lambda s, role, t, value, before: (t.row in s.ids(t.table), False),
    ),
    Door(
        operation="QueryTable",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].query_table(s.query()),
        shows=lambda s, role, t, value, before: (_queried_ids(value.data), [0, 1, 2]),
    ),
    Door(
        operation="CountTableRows",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].count_table_rows(ln.CountTableRowsRequest(id=t.table)),
        shows=lambda s, role, t, value, before: (value.count, s.rows(t.table)),
    ),
    Door(
        operation="CreateTable",
        rung=Rung.WRITER,
        aim=lambda s, role, allowed: Target(table=[s.namespace, f"made-{role}"]),
        send=lambda s, role, t: s.clients[role].create_table(ln.CreateTableRequest(id=t.table), scenario.arrow_ipc(scenario.rows_of(range(3)))),
        watch=lambda s, t: s.exists(t.table),
        shows=lambda s, role, t, value, before: (s.ids(t.table), [0, 1, 2]),
    ),
    Door(
        operation="ExplainTableQueryPlan",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].explain_table_query_plan(ln.ExplainTableQueryPlanRequest(id=t.table, query=s.query())),
        shows=lambda s, role, t, value, before: (("LanceRead" in value, "projection=[id]" in value), (True, True)),
    ),
    Door(
        operation="AnalyzeTableQueryPlan",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].analyze_table_query_plan(
            ln.AnalyzeTableQueryPlanRequest(id=t.table, k=10, vector=wire.QueryTableRequestVector(single_vector=[]), filter="id < 3")
        ),
        shows=lambda s, role, t, value, before: (("AnalyzeExec" in value, "output_rows=3" in value), (True, True)),
    ),
    Door(
        operation="AlterTableAddColumns",
        rung=Rung.WRITER,
        aim=_at_schema,
        send=lambda s, role, t: s.clients[role].alter_table_add_columns(
            ln.AlterTableAddColumnsRequest(id=t.table, new_columns=[wire.AddColumnsEntry(name=f"added_{_slug(role)}", expression="id * 2")])
        ),
        watch=lambda s, t: s.columns(t.table),
        shows=lambda s, role, t, value, before: (f"added_{_slug(role)}" in s.columns(t.table), True),
    ),
    Door(
        operation="AlterTableBackfillColumns",
        rung=Rung.WRITER,
        declined=True,
        aim=_at_schema,
        send=lambda s, role, t: s.clients[role].alter_table_backfill_columns(ln.AlterTableBackfillColumnsRequest(id=t.table, column="payload")),
        watch=lambda s, t: s.latest(t.table),
    ),
    # A refresh needs `can_refresh` on the view, which a view's creation grants. CreateMaterializedView is declined, so
    # no view exists to hold it, and a refresh is no read: the catalog answers an absent object a refusal, never a 404.
    Door(
        operation="RefreshMaterializedView",
        rung=Rung.ABOVE,
        aim=lambda s, role, allowed: Target(table=[s.namespace, "view"]),
        send=lambda s, role, t: s.clients[role].refresh_materialized_view(ln.RefreshMaterializedViewRequest(id=t.table)),
        watch=lambda s, t: s.tables(),
    ),
    Door(
        operation="CreateMaterializedView",
        rung=Rung.WRITER,
        declined=True,
        aim=lambda s, role, allowed: Target(table=[s.namespace, f"view-{role}"]),
        send=lambda s, role, t: s.clients[role].create_materialized_view(
            ln.CreateMaterializedViewRequest(id=t.table, kind="query", source_query="SELECT id FROM t", output_schema="{}")
        ),
        watch=lambda s, t: s.tables(),
    ),
    # The lakehouse builds no index (ADR 0162): the caller's own compute builds it and commits through version/create.
    Door(
        operation="CreateTableIndex",
        rung=Rung.WRITER,
        declined=True,
        send=lambda s, role, t: s.clients[role].create_table_index(ln.CreateTableIndexRequest(id=t.table, column="id", index_type="BTREE")),
        watch=lambda s, t: (s.latest(t.table), s.indices(t.table)),
    ),
    Door(
        operation="CreateTableScalarIndex",
        rung=Rung.WRITER,
        declined=True,
        send=lambda s, role, t: s.clients[role].create_table_scalar_index(ln.CreateTableIndexRequest(id=t.table, column="id", index_type="BTREE")),
        watch=lambda s, t: (s.latest(t.table), s.indices(t.table)),
    ),
    Door(
        operation="ListTableIndices",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].list_table_indices(ln.ListTableIndicesRequest(id=t.table)),
        shows=lambda s, role, t, value, before: (sorted(index.index_name for index in value.indexes), s.indices(t.table)),
    ),
    Door(
        operation="DescribeTableIndexStats",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].describe_table_index_stats(ln.DescribeTableIndexStatsRequest(id=t.table, index_name="id_idx")),
        shows=lambda s, role, t, value, before: ((value.index_type, value.num_indices), ("BTree", 1)),
    ),
    Door(
        operation="DropTableIndex",
        rung=Rung.WRITER,
        aim=_fresh_index,
        send=lambda s, role, t: s.clients[role].drop_table_index(ln.DropTableIndexRequest(id=t.table, index_name="id_idx")),
        watch=lambda s, t: s.indices(t.table),
        shows=lambda s, role, t, value, before: ("id_idx" in s.indices(t.table), False),
    ),
    Door(
        operation="ListTableTags",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].list_table_tags(ln.ListTableTagsRequest(id=t.table)),
        shows=lambda s, role, t, value, before: (sorted(value.tags), sorted(s.tags(t.table))),
    ),
    Door(
        operation="GetTableTagVersion",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].get_table_tag_version(ln.GetTableTagVersionRequest(id=t.table, tag="keep")),
        shows=lambda s, role, t, value, before: (value.version, 1),
    ),
    Door(
        operation="DeclareTable",
        rung=Rung.WRITER,
        aim=lambda s, role, allowed: Target(table=[s.namespace, f"declared-{role}"]),
        send=lambda s, role, t: s.clients[role].declare_table(ln.DeclareTableRequest(id=t.table)),
        watch=lambda s, t: s.tables(declared=True),
        shows=lambda s, role, t, value, before: (
            (t.table[-1] in s.tables(declared=True), {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"} <= set(value.storage_options or {})),
            (True, True),
        ),
    ),
    Door(
        operation="CreateTableTag",
        rung=Rung.OWNER,
        aim=lambda s, role, allowed: Target(table=s.table, name=f"made-{role}"),
        send=lambda s, role, t: s.clients[role].create_table_tag(ln.CreateTableTagRequest(id=t.table, tag=t.name, version=1)),
        watch=lambda s, t: s.tags(t.table),
        shows=lambda s, role, t, value, before: (s.tags(t.table).get(t.name), 1),
    ),
    # A deleted tag reaches the stock client as the spec's typed TableTagNotFound, code 8.
    Door(
        operation="DeleteTableTag",
        rung=Rung.OWNER,
        aim=_fresh_tag("gone"),
        send=lambda s, role, t: s.clients[role].delete_table_tag(ln.DeleteTableTagRequest(id=t.table, tag=t.name)),
        watch=lambda s, t: s.tags(t.table),
        shows=lambda s, role, t, value, before: ((t.name in s.tags(t.table), _tag_code(s, t.name)), (False, TABLE_TAG_NOT_FOUND)),
    ),
    Door(
        operation="UpdateTableTag",
        rung=Rung.OWNER,
        aim=_fresh_tag("moved"),
        send=lambda s, role, t: s.clients[role].update_table_tag(ln.UpdateTableTagRequest(id=t.table, tag=t.name, version=2)),
        watch=lambda s, t: s.tags(t.table),
        shows=lambda s, role, t, value, before: (s.tags(t.table).get(t.name), 2),
    ),
    Door(
        operation="ListTableBranches",
        rung=Rung.READER,
        send=lambda s, role, t: s.clients[role].list_table_branches(ln.ListTableBranchesRequest(id=t.table)),
        shows=lambda s, role, t, value, before: (sorted(value.branches), s.branches(t.table)),
    ),
    Door(
        operation="CreateTableBranch",
        rung=Rung.OWNER,
        aim=lambda s, role, allowed: Target(table=s.table, name=f"made-{role}"),
        send=lambda s, role, t: s.clients[role].create_table_branch(ln.CreateTableBranchRequest(id=t.table, name=t.name)),
        watch=lambda s, t: s.branches(t.table),
        shows=lambda s, role, t, value, before: (t.name in s.branches(t.table), True),
    ),
    Door(
        operation="DeleteTableBranch",
        rung=Rung.OWNER,
        aim=_fresh_branch,
        send=lambda s, role, t: s.clients[role].delete_table_branch(ln.DeleteTableBranchRequest(id=t.table, name=t.name)),
        watch=lambda s, t: s.branches(t.table),
        shows=lambda s, role, t, value, before: (t.name in s.branches(t.table), False),
    ),
    # A table's transaction is `<table id>$<transaction id>` on the dir backend. Describing it is a read of the table
    # (can_get_metadata), and the backend applies no alteration, so alter is declined past the table's writer rung
    # (LH-221's How). A committed restore's transaction has succeeded: the spec's TransactionStatus is case-insensitive.
    Door(
        operation="DescribeTransaction",
        rung=Rung.READER,
        aim=lambda s, role, allowed: Target(table=s.txn),
        send=lambda s, role, t: s.clients[role].describe_transaction(ln.DescribeTransactionRequest(id=[*t.table, s.transaction])),
        shows=lambda s, role, t, value, before: (str(value.status).lower(), "succeeded"),
    ),
    Door(
        operation="AlterTransaction",
        rung=Rung.WRITER,
        declined=True,
        aim=lambda s, role, allowed: Target(table=s.txn),
        send=lambda s, role, t: s.clients[role].alter_transaction(
            ln.AlterTransactionRequest(
                id=[*t.table, s.transaction],
                actions=[wire.AlterTransactionAction(set_status_action=wire.AlterTransactionSetStatus(status="Canceled"))],
            )
        ),
        watch=lambda s, t: s.latest(t.table),
    ),
)

#: rask's own doors around the spec's: the vend, the grant and revoke of a namespace rung, the revoke of a label's
#: `apply`, and the warehouse's and the project's admin doors. A delete of the warehouse or the project answers a
#: refused caller the spec's not-found, so it
#: tells nobody whether the object exists; it names no cascade, so a wrongly allowed one stops at what the tenant holds.
MANAGEMENT_DOORS: tuple[Door, ...] = (
    Door(
        operation="VendReadCredentials",
        rung=Rung.READER,
        refused=(403, PERMISSION_DENIED),
        send=_vend("read"),
        shows=_vended,
    ),
    Door(
        operation="VendWriteCredentials",
        rung=Rung.WRITER,
        refused=(403, PERMISSION_DENIED),
        send=_vend("write"),
        shows=_vended,
    ),
    Door(
        operation="GrantNamespaceRung",
        rung=Rung.OWNER,
        refused=(403, PERMISSION_DENIED),
        aim=_fresh_child("ns-granted"),
        send=_access_door("grant", "writer"),
        watch=lambda s, t: s.access(t.namespace),
        shows=lambda s, role, t, value, before: (s.granted(t.namespace, "writer"), True),
    ),
    Door(
        operation="RevokeNamespaceRung",
        rung=Rung.OWNER,
        refused=(403, PERMISSION_DENIED),
        aim=_fresh_child("ns-revoked", granted="reader"),
        send=_access_door("revoke", "reader"),
        watch=lambda s, t: s.access(t.namespace),
        shows=lambda s, role, t, value, before: (s.granted(t.namespace, "reader"), False),
    ),
    Door(
        operation="RevokeClassificationApply",
        rung=Rung.OWNER,
        refused=(403, PERMISSION_DENIED),
        aim=_fresh_label_holder,
        send=_revoke_label_apply,
        watch=lambda s, t: s.label_holders("apply"),
        shows=lambda s, role, t, value, before: (any(t.name in user for user in s.label_holders("apply")), False),
    ),
    Door(
        operation="CreateWarehouseNamespace",
        rung=Rung.ABOVE,
        refused=(403, PERMISSION_DENIED),
        names_tenant=True,
        aim=lambda s, role, allowed: Target(name=f"c2w-{role}-{s.namespace}"),
        send=lambda s, role, t: s.http(role).post(
            f"{CATALOG}/management/v1/warehouses/{s.tenant.project}/namespaces", json={"namespace": t.name, "properties": {"rask.tier": "bronze"}}, timeout=60
        ),
        watch=_warehouse_namespaces,
    ),
    Door(
        operation="DeleteWarehouse",
        rung=Rung.ABOVE,
        refused=(404, TABLE_NOT_FOUND),
        names_tenant=True,
        send=lambda s, role, t: s.http(role).delete(f"{CATALOG}/management/v1/warehouses/{s.tenant.project}", timeout=60),
        watch=lambda s, t: s.as_admin(f"/management/v1/warehouses/{s.tenant.project}").status_code,
    ),
    Door(
        operation="CreateProjectWarehouse",
        rung=Rung.ABOVE,
        refused=(403, PERMISSION_DENIED),
        names_tenant=True,
        send=lambda s, role, t: s.http(role).post(
            f"{CATALOG}/management/v1/warehouses", json={"id": f"c2w-{role}-{s.namespace}", "project": s.tenant.project}, timeout=120
        ),
        watch=_project_warehouses,
    ),
    Door(
        operation="GrantProjectRole",
        rung=Rung.ABOVE,
        refused=(403, PERMISSION_DENIED),
        names_tenant=True,
        send=lambda s, role, t: s.http(role).put(
            f"{CATALOG}/management/v1/projects/{s.tenant.project}/members", json={"user": s.grantee, "relation": "member"}, timeout=60
        ),
        watch=_project_members,
    ),
    Door(
        operation="DeleteProject",
        rung=Rung.ABOVE,
        refused=(404, TABLE_NOT_FOUND),
        names_tenant=True,
        send=lambda s, role, t: s.http(role).delete(f"{CATALOG}/management/v1/projects/{s.tenant.project}", timeout=60),
        watch=lambda s, t: s.as_admin(f"/management/v1/projects/{s.tenant.project}").status_code,
    ),
)

DOORS: dict[str, Door] = {door.operation: door for door in (*SPEC_DOORS, *MANAGEMENT_DOORS)}

#: Every operation the spec defines, in its order: each is a row of the matrix, and one without a door is a red row.
OPERATIONS: list[str] = [
    operation["operationId"]
    for path in yaml.safe_load(SPEC.read_text(encoding="utf-8"))["paths"].values()
    for operation in path.values()
    if "operationId" in operation
]

CELLS = [
    pytest.param(operation, role, id=f"{operation}-{role}") for operation in (*OPERATIONS, *(door.operation for door in MANAGEMENT_DOORS)) for role in Role
]


@pytest.fixture(scope="module")
def tenant_b(byo_tenant: Tenant) -> Iterator[str]:
    """A second tenant, administered by bob: the estate admin makes the project and bob its admin, and bob makes its
    warehouse, which only an admin of that project may do."""
    admin, bob = byo_tenant.admin, byo_tenant.validator
    project = f"c2b-{uuid.uuid4().hex[:8]}"
    scenario.ok(admin.http.post(f"{CATALOG}/management/v1/projects", json={"id": project}, timeout=60))
    try:
        scenario.ok(admin.http.put(f"{CATALOG}/management/v1/projects/{project}/members", json={"user": bob.principal, "relation": "admin"}, timeout=60))
        made = {"id": project, "project": project}
        eventually(
            lambda: bob.http.post(f"{CATALOG}/management/v1/warehouses", json=made, timeout=120).status_code == 200,
            f"bob, made admin of {project}, did not make its warehouse",
        )
        yield project
    finally:
        bob.http.delete(f"{CATALOG}/management/v1/warehouses/{project}", params={"cascade": "true", "purge_bucket": "true"}, timeout=120)
        admin.http.delete(f"{CATALOG}/management/v1/projects/{project}", timeout=60)


@pytest.fixture(scope="module")
def scene(byo_tenant: Tenant, tenant_b: str) -> Iterator[Scene]:
    """The matrix's scene on the shared tenant, its owner the scenario's pipeline and its tenant-B admin bob. The
    tenant's cascade removes what the scene made in its warehouse; the matrix label it removes itself."""
    service = Identity(Path(SERVICE_TOKEN_FILE).read_text(encoding="utf-8").strip())
    assert service.principal.startswith("kubernetes~system:serviceaccount:"), f"the service token proves {service.principal}, which is no service account"
    callers = {
        Role.OWNER: byo_tenant.pipeline,
        Role.WRITER: Identity.person(WRITER_USER),
        Role.READER: Identity.person(READER_USER),
        Role.OUTSIDER: Identity.person(OUTSIDER_USER),
        Role.TENANT_B_ADMIN: byo_tenant.validator,
        Role.SERVICE: service,
    }
    made = Scene(byo_tenant, callers, tenant_b)
    try:
        made.build()
        yield made
    finally:
        left = made.remove_label()
        assert not left, f"the matrix label {made.label} still holds {left}"


@pytest.mark.parametrize(("operation", "role"), CELLS)
def test_each_door_answers_each_principal_what_its_rung_owes(scene: Scene, operation: str, role: Role) -> None:
    """An allowed call round-trips, or a declined door answers the spec's code 0. A refused call answers the door's
    refusal, leaves what the door could change as it was, and names nothing the caller did not send."""
    door = DOORS[operation]
    allowed = HOLDS[role] >= door.rung
    target = door.aim(scene, role, allowed)
    before = door.watch(scene, target)

    answer = attempt(lambda: door.send(scene, role, target))

    if not allowed:
        assert (answer.status, answer.code) == door.refused, answer.text
        assert door.watch(scene, target) == before, f"a refused {operation} changed what it acts on"
        assert not {name for name in scene.foreign(role, names_tenant=door.names_tenant) if name and name in answer.text}, answer.text
    elif door.declined:
        assert answer.code == UNSUPPORTED, answer.text
        assert door.watch(scene, target) == before, f"a declined {operation} changed what it acts on"
    else:
        assert answer.code is None, answer.text
        observed, expected = door.shows(scene, role, target, answer.value, before)
        assert observed == expected


def _unchanged(scene: Scene) -> None:
    """A rule whose answer changes nothing a later read could tell apart."""


class Rule(BaseModel):
    """A governance rule a caller who clears a door's gate still meets: the request as its caller sends it, the spec code
    it answers (None where the rule's answer is the request's success), and what it must leave as it was."""

    model_config = ConfigDict(frozen=True)

    send: Callable[[Scene], Any]
    code: int | None
    watch: Callable[[Scene], object] = _unchanged


def _unapproved_blob_base(scene: Scene) -> requests.Response:
    """A create naming an external blob base outside every approved one, for a table with a blob-v2 column. The base is
    rask's own query parameter, which the spec's request model does not carry, so the request is plain HTTP."""
    rows = pa.table({"id": pa.array([0], pa.int64()), "image": lance.blob_array([b"bytes"])})
    return scene.http(Role.OWNER).post(
        f"{CATALOG}/v1/table/{scene.namespace}$blob-refused/create",
        params={"external_blob_base": "s3://c2-unapproved/blobs/"},
        data=scenario.arrow_ipc(rows),
        headers={"content-type": scenario.ARROW},
        timeout=120,
    )


#: The rules rask's hierarchy and the closed rows made, each met by a caller who clears the door's gate: the tenant's
#: admin at the hierarchy's doors, the matrix namespace's owner, its writer at the provenance guard, and the tenant-B
#: admin, in its own project, at the warehouse door.
RULES: dict[str, Rule] = {
    "hierarchy-root-table": Rule(
        send=lambda s: s.tenant.admin.namespace().create_table(ln.CreateTableRequest(id=[f"root-{s.suffix}"]), scenario.arrow_ipc(scenario.rows_of(range(1)))),
        code=INVALID_INPUT,
    ),
    "hierarchy-top-level-namespace": Rule(
        send=lambda s: s.tenant.admin.namespace().create_namespace(ln.CreateNamespaceRequest(id=[f"top-{s.suffix}"])),
        code=INVALID_INPUT,
    ),
    "hierarchy-populated-namespace-drop": Rule(
        send=lambda s: s.owner.drop_namespace(ln.DropNamespaceRequest(id=[s.namespace])),
        code=NAMESPACE_NOT_EMPTY,
        watch=lambda s: s.tables(),
    ),
    # spec.yaml CreateNamespaceRequest.mode: an Overwrite replaces the namespace, which rask holds to Restrict.
    "LH-037-overwrite-of-a-populated-namespace": Rule(
        send=lambda s: s.owner.create_namespace(ln.CreateNamespaceRequest(id=s.full, mode="Overwrite")),
        code=NAMESPACE_NOT_EMPTY,
        watch=lambda s: sorted(s.owner.list_tables(ln.ListTablesRequest(id=s.full)).tables),
    ),
    "LH-037-overwrite-of-an-empty-namespace": Rule(
        send=lambda s: s.owner.create_namespace(ln.CreateNamespaceRequest(id=s.empty, mode="Overwrite")),
        code=None,
        watch=lambda s: s.children(),
    ),
    "LH-203-a-branch-named-into-a-layout-directory": Rule(
        send=lambda s: s.owner.create_table_branch(ln.CreateTableBranchRequest(id=s.table, name="layout/_versions")),
        code=INVALID_INPUT,
        watch=lambda s: s.branches(s.table),
    ),
    "LH-203-a-branch-nested-in-another": Rule(
        send=lambda s: s.owner.create_table_branch(ln.CreateTableBranchRequest(id=s.table, name="keep/inner")),
        code=INVALID_TABLE_STATE,
        watch=lambda s: s.branches(s.table),
    ),
    "LH-204-a-register-at-a-held-location": Rule(
        send=lambda s: s.owner.register_table(ln.RegisterTableRequest(id=[s.namespace, "alias"], location=s.held)),
        code=INVALID_INPUT,
        watch=lambda s: s.exists([s.namespace, "alias"]),
    ),
    # A table the catalog makes lives in a uuid directory directly under the root, which no registrar may name ([[LH-520]]).
    "LH-520-a-register-in-the-catalogs-layout": Rule(
        send=lambda s: s.owner.register_table(ln.RegisterTableRequest(id=[s.namespace, "layout"], location=s.location.rstrip("/").rsplit("/", 1)[-1])),
        code=INVALID_INPUT,
        watch=lambda s: s.exists([s.namespace, "layout"]),
    ),
    "LH-205-a-warehouse-in-another-store": Rule(
        send=lambda s: s.http(Role.TENANT_B_ADMIN).post(
            f"{CATALOG}/management/v1/warehouses",
            json={"id": f"c2x-{s.suffix}", "project": s.tenant_b, "endpoint": "http://foreign-store.invalid:9000"},
            timeout=120,
        ),
        code=INVALID_INPUT,
        watch=lambda s: s.warehouses_of(s.tenant_b),
    ),
    "LH-208-a-writer-setting-a-provenance-key": Rule(
        send=lambda s: s.clients[Role.WRITER].update_table_schema_metadata(ln.UpdateTableSchemaMetadataRequest(id=s.table, metadata={"rask.run_id": "forged"})),
        code=INVALID_INPUT,
        watch=lambda s: s.latest(s.table),
    ),
    "LH-209-an-unapproved-external-blob-base": Rule(
        send=_unapproved_blob_base,
        code=INVALID_INPUT,
        watch=lambda s: s.exists([s.namespace, "blob-refused"]),
    ),
}


@pytest.mark.parametrize("case", list(RULES))
def test_a_governance_rule_answers_a_caller_who_clears_the_gate(scene: Scene, case: str) -> None:
    """A refusal reaches the caller as the spec's typed error, carrying the code a client dispatches on, and changes
    nothing; a rule whose answer is the request's success leaves what it acts on standing."""
    rule = RULES[case]
    before = rule.watch(scene)

    answer = attempt(lambda: rule.send(scene))

    assert (answer.code, rule.watch(scene)) == (rule.code, before), answer.text


def test_lancedb_opens_a_table_through_the_namespace_and_reads_its_rows(scene: Scene) -> None:
    """The reader opens the matrix table with lancedb's namespace connection, which resolves the table and its
    credential through the catalog, and reads the rows the owner's pylance reads."""
    import lancedb

    database = lancedb.namespace.connect_namespace("rest", scene.properties(Role.READER))

    read = database.open_table(scene.table[-1], namespace_path=[scene.namespace]).to_arrow()

    assert sorted(read.column("id").to_pylist()) == scene.ids(scene.table)


def test_lance_ray_reads_a_table_through_the_namespace(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader reads the matrix table with lance-ray, which resolves it through the catalog, on a local Ray.

    Ray's uv integration starts each worker with the driver's own `uv run` command line when the driver runs under
    `uv run`; under pytest that worker never starts. Measured on Ray 2.58 2026-10-17: five failed worker starts and a
    nine-minute wait, against 2.9 s with the integration off, so the local Ray here runs its workers on this interpreter.
    """
    import lance_ray
    import ray

    monkeypatch.setenv("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    ray.init(address="local", num_cpus=2, include_dashboard=False)
    try:
        read = lance_ray.read_lance(table_id=scene.table, namespace_impl="rest", namespace_properties=scene.properties(Role.READER))
        ids = sorted(int(row["id"]) for row in read.take_all())
    finally:
        ray.shutdown()

    assert ids == scene.ids(scene.table)
