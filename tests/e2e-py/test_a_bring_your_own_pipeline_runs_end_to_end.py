"""A bring-your-own pipeline, end to end on the live estate ([[LH-478]], ADRs 0154 to 0158 and 0161).

The lakehouse runs no pipeline. A user's job, on its own compute, writes every tier through the catalog's Lance
Namespace doors with a stock client, follows the commit feed to learn what to do next, and publishes through the
tag gate. This lane is that job, run from outside the cluster:

1. it seeds bronze with stock pylance through the namespace, on the credential the catalog vends;
2. it follows lineage's `GET /events?since=` forward to the bronze write and the version it committed;
3. it derives silver through the catalog's create and insert doors, naming its run and the bronze input at that
   version in the spec Context (`x-rask-run-id`, `x-rask-job`, `x-rask-inputs`);
4. it publishes: the project's gate holds a breaching version behind `published`, readers of `@published` keep the
   previous version, a validator approves, and the project's watcher is told;
5. it follows the feed over silver and reads its writes and both publications in commit order.

A second leg holds the write tier's vended credential and tries to forge the `published` tag in the store itself.

Three Dex users from the deployed configmap play the parts. The estate admin creates a scratch tenant (a project,
a warehouse with its own bucket, a bronze and a silver namespace), declares its gate and watches it. The pipeline
identity holds only `writer` on the two namespaces, so it owns what it creates. The validator holds `validator` on
silver. The tenant is deleted at the end.

Run with `bash scripts/e2e_live.sh tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py`, which discovers
every address from the release, or `make e2e-byo-pipeline` with the addresses set.
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import lance
import lance_namespace
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
import requests
from botocore.config import Config
from botocore.exceptions import ClientError
from principals import DEX_SCOPE, principal_of


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "").rstrip("/")
#: The notifications service under its API prefix (`http://<host>:8850/api`).
NOTIFICATIONS = os.environ.get("LANCE_E2E_NOTIFICATIONS_URL", "").rstrip("/")
DEX = os.environ.get("LANCE_E2E_DEX", "").rstrip("/")
DEX_CLIENT = os.environ.get("LANCE_E2E_OIDC_CLIENT_ID", "lance-catalog")
DEX_SECRET = os.environ.get("LANCE_E2E_DEX_SECRET", "lance-catalog-secret")
ADMIN_USER = os.environ.get("LANCE_E2E_USER", "alice@example.com")
PIPELINE_USER = os.environ.get("LANCE_E2E_PIPELINE_USER", "publisher@rask.internal")
VALIDATOR_USER = os.environ.get("LANCE_E2E_VALIDATOR_USER", "bob@example.com")
#: How long a committed write may take to reach lineage's feed and an inbox.
SETTLE_SECONDS = float(os.environ.get("LANCE_E2E_SETTLE_SECONDS", "120"))
ARROW = "application/vnd.apache.arrow.stream"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.byo_pipeline,
    pytest.mark.skipif(
        not (CATALOG and LINEAGE and NOTIFICATIONS and DEX),
        reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL and LANCE_E2E_DEX (a deployed estate)",
    ),
]


class Identity:
    """One Dex user: its bearer, and the principal the estate's grants name ([[LH-063]])."""

    def __init__(self, email: str) -> None:
        answer = requests.post(
            f"{DEX}/token",
            data={
                "grant_type": "password",
                "client_id": DEX_CLIENT,
                "client_secret": DEX_SECRET,
                "username": email,
                "password": "password",
                "scope": DEX_SCOPE,
            },
            timeout=20,
        )
        answer.raise_for_status()
        self.bearer = str(answer.json()["id_token"])
        self.principal = principal_of(CATALOG, self.bearer)
        self.http = requests.Session()
        self.http.headers["authorization"] = f"Bearer {self.bearer}"

    def namespace(self, **context: str) -> lance_namespace.LanceNamespace:
        """The stock client an outside pipeline gets, sending ``context`` as spec Context headers on every call."""
        headers = {f"headers.{name}": value for name, value in context.items()}
        return lance_namespace.connect("rest", {"uri": CATALOG, "headers.Authorization": f"Bearer {self.bearer}", **headers})


def _ok(answer: requests.Response) -> dict[str, Any]:
    assert answer.status_code == 200, f"{answer.request.method} {answer.request.url} -> {answer.status_code}: {answer.text[:500]}"
    return answer.json()


def _rows(ids: range) -> pa.Table:
    return pa.table({"id": pa.array(list(ids), pa.int64()), "payload": pa.array([f"row-{i}" for i in ids])})


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


class Tenant:
    """The scratch tenant one run drives, and what it must take down again."""

    def __init__(self) -> None:
        self.admin = Identity(ADMIN_USER)
        self.pipeline = Identity(PIPELINE_USER)
        self.validator = Identity(VALIDATOR_USER)
        self.project = f"byo-{uuid.uuid4().hex[:8]}"
        self.bronze = f"{self.project}-bronze"
        self.silver = f"{self.project}-silver"
        self.tables: list[str] = []
        self.watching = False

    def create(self) -> None:
        admin = self.admin
        _ok(admin.http.post(f"{CATALOG}/management/v1/projects", json={"id": self.project}))
        _ok(admin.http.post(f"{CATALOG}/management/v1/warehouses", json={"id": self.project, "project": self.project}))
        for namespace, tier in ((self.bronze, "bronze"), (self.silver, "silver")):
            body = {"namespace": namespace, "properties": {"rask.tier": tier}}
            _ok(admin.http.post(f"{CATALOG}/management/v1/warehouses/{self.project}/namespaces", json=body))
            _ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{namespace}/access/grant", json={"user": self.pipeline.principal, "relation": "writer"}))
        _ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{self.silver}/access/grant", json={"user": self.validator.principal, "relation": "validator"}))
        self.declare_gate(review=False)

    def declare_gate(self, *, review: bool) -> None:
        body = {"key_column": "id", "review_band": 0.25, "review_enabled": review}
        _ok(self.admin.http.post(f"{CATALOG}/management/v1/project/{self.project}/gate/set", json=body))

    def watch(self) -> None:
        _ok(self.admin.http.put(f"{NOTIFICATIONS}/notifications/watches/{self.project}", timeout=30))
        self.watching = True

    def tear_down(self) -> list[str]:
        """Drop every table the pipeline made, then the warehouse with its bucket, then the project; what failed."""
        failed: list[str] = []
        attempts: list[tuple[str, Callable[[], requests.Response]]] = []
        if self.watching:
            attempts.append(("watch", lambda: self.admin.http.delete(f"{NOTIFICATIONS}/notifications/watches/{self.project}", timeout=30)))
        attempts += [(table, lambda t=table: self.pipeline.http.post(f"{CATALOG}/v1/table/{t}/drop", json={}, timeout=60)) for table in self.tables]
        attempts.append(
            (
                "warehouse",
                lambda: self.admin.http.delete(
                    f"{CATALOG}/management/v1/warehouses/{self.project}", params={"cascade": "true", "purge_bucket": "true"}, timeout=120
                ),
            )
        )
        attempts.append(("project", lambda: self.admin.http.delete(f"{CATALOG}/management/v1/projects/{self.project}", timeout=60)))
        for what, attempt in attempts:
            answer = attempt()
            if answer.status_code != 200:
                failed.append(f"{what}: {answer.status_code} {answer.text[:200]}")
        return failed


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    made = Tenant()
    try:
        made.create()
        yield made
    finally:
        left = made.tear_down()
        assert not left, f"the scratch tenant {made.project} was not fully removed: {left}"


def _lance(row: dict[str, Any]) -> dict[str, Any]:
    """The `lance` facet of a feed row: on the run of a write, on the dataset of a definition change."""
    event = row.get("event") or {}
    carrier = event.get("run") or event.get("dataset") or {}
    return (carrier.get("facets") or {}).get("lance") or {}


def _feed_start(identity: Identity) -> int:
    """Where a pipeline that has read nothing yet starts following: the newest row it may see, else the oldest held."""
    body = _ok(identity.http.get(f"{LINEAGE}/events", params={"limit": 1}, timeout=30))
    events = body.get("events") or []
    return int(events[0]["seq"]) if events else max(int(body.get("oldest_seq") or 1) - 1, 0)


def _follow(identity: Identity, *, since: int, table: str, until: Callable[[list[dict[str, Any]]], bool]) -> tuple[list[dict[str, Any]], int]:
    """Follow the feed forward from ``since`` the way the contract says (`docs/COMMIT-EVENTS.md`): pass `next_cursor`
    back as `since` every time, until the rows read so far satisfy ``until``. The rows, and the cursor to resume from."""
    rows: list[dict[str, Any]] = []
    cursor = since
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        body = _ok(identity.http.get(f"{LINEAGE}/events", params={"since": cursor, "table": table, "limit": 500}, timeout=30))
        rows += body["events"]
        assert int(body["next_cursor"]) >= cursor, f"the cursor moved backwards: {cursor} -> {body['next_cursor']}"
        cursor = int(body["next_cursor"])
        if until(rows):
            return rows, cursor
        assert time.monotonic() < deadline, f"the feed for {table} did not show what was expected within {SETTLE_SECONDS:.0f} s: {[_lance(r) for r in rows]}"
        time.sleep(2)


def _writes(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """``(operation, version)`` of each row that names a committed version, in feed order."""
    return [(str(_lance(row).get("operation")), int(_lance(row)["version"])) for row in rows if "version" in _lance(row)]


def _published_rows(identity: Identity, table_id: list[str]) -> int:
    """What a stock reader of `@published` sees: the row count of the version the tag names."""
    return lance.dataset(namespace_client=identity.namespace(), table_id=table_id, version="published").count_rows()


def test_a_pipeline_seeds_bronze_derives_silver_and_its_breach_waits_for_a_validator(tenant: Tenant) -> None:
    pipeline, validator, admin = tenant.pipeline, tenant.validator, tenant.admin
    bronze_id, silver_id = [tenant.bronze, "orders"], [tenant.silver, "orders"]
    bronze_table, silver_table = "$".join(bronze_id), "$".join(silver_id)
    start = _feed_start(pipeline)

    # 1. Seed bronze with stock pylance through the namespace: a first load, then an increment.
    stock = pipeline.namespace()
    lance.write_dataset(_rows(range(4)), namespace_client=stock, table_id=bronze_id, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    tenant.tables.append(bronze_table)
    lance.write_dataset(_rows(range(4, 8)), namespace_client=stock, table_id=bronze_id, mode="append")

    # 2. Follow the feed to the bronze write: its committed version is the input silver derives from.
    bronze_rows, _ = _follow(pipeline, since=start, table=bronze_table, until=lambda rows: ("insert", 2) in _writes(rows))
    bronze_version = max(version for operation, version in _writes(bronze_rows) if operation == "insert")
    assert bronze_version == 2

    # 3. Derive silver through the catalog's doors, naming the run and the input in the spec Context.
    def silver_run(run_id: str) -> lance_namespace.LanceNamespace:
        inputs = json.dumps([{"table": bronze_id, "version": bronze_version}])
        return pipeline.namespace(**{"x-rask-run-id": run_id, "x-rask-job": "byo-silver", "x-rask-inputs": inputs})

    silver_start = _feed_start(pipeline)
    silver_run("byo-run-1").create_table(lance_namespace.CreateTableRequest(id=silver_id, mode="create"), _ipc(_rows(range(8))))
    tenant.tables.append(silver_table)
    first = _ok(pipeline.http.post(f"{CATALOG}/management/v1/table/{silver_table}/publish", json={"version": 1, "key_column": "id"}, timeout=120))
    assert (first["published"], first["held"]) == (True, False), first

    # 4. The project turns review on; the next batch triples silver, past the 0.25 band, and is held behind `published`.
    tenant.declare_gate(review=True)
    silver_run("byo-run-2").insert_into_table(lance_namespace.InsertIntoTableRequest(id=silver_id, mode="append"), _ipc(_rows(range(8, 24))))
    held = _ok(pipeline.http.post(f"{CATALOG}/management/v1/table/{silver_table}/publish", json={"version": 2, "key_column": "id"}, timeout=120))
    assert (held["published"], held["held"], held["hold"]["reasons"]) == (False, True, ["row_count_delta"]), held
    assert _published_rows(admin, silver_id) == 8, "a reader of @published saw the held version"
    assert lance.dataset(namespace_client=stock, table_id=silver_id).count_rows() == 24

    # The validator approves; the admin, watching from just before, is told by the approval's announcement.
    tenant.watch()
    approved = _ok(validator.http.post(f"{CATALOG}/management/v1/table/{silver_table}/promotion/approve", json={"version": 2}, timeout=120))
    assert (approved["published_version"], approved["hold"]["decided_by"]) == (2, validator.principal), approved
    assert _published_rows(admin, silver_id) == 24

    # 5. Following silver's feed reads its writes and both publications in commit order.
    silver_rows, cursor = _follow(pipeline, since=silver_start, table=silver_table, until=lambda rows: ("publish_table", 2) in _writes(rows))
    assert [int(row["seq"]) for row in silver_rows] == sorted(int(row["seq"]) for row in silver_rows)
    assert [write for write in _writes(silver_rows) if write[0] in ("insert", "publish_table")] == [("publish_table", 1), ("insert", 2), ("publish_table", 2)]
    (insert,) = [row for row in silver_rows if _lance(row).get("operation") == "insert"]
    assert (insert["inputs"], _lance(insert)["runId"], _lance(insert)["job"]) == ([bronze_table], "byo-run-2", "byo-silver"), insert
    caught_up = _ok(pipeline.http.get(f"{LINEAGE}/events", params={"since": cursor, "table": silver_table}, timeout=30))
    assert (caught_up["events"], caught_up["next_cursor"]) == ([], cursor), caught_up

    (announced,) = [row for row in silver_rows if _writes([row]) == [("publish_table", 2)]]
    notification = f"{announced['event']['run']['runId']}@COMPLETE"
    deadline = time.monotonic() + SETTLE_SECONDS
    told: list[dict[str, Any]] = []
    while not told:
        inbox = _ok(admin.http.get(f"{NOTIFICATIONS}/notifications/inbox", params={"limit": 100}, timeout=30))
        told = [row for row in inbox["notifications"] if row["notification_id"] == notification]
        assert told or time.monotonic() < deadline, f"the watcher's inbox never received {notification}"
        if not told:
            time.sleep(3)
    assert [(row["reason"], row["object_id"]) for row in told] == [("watch", silver_table)]


def test_the_write_tier_credential_cannot_forge_the_published_tag(tenant: Tenant) -> None:
    """The pipeline owns its table, so `DescribeTable` vends it the write tier. That credential reads the table's
    prefix and puts under `data/`, and the store refuses its PUT of `_refs/tags/published.json`: only the catalog
    moves a tag."""
    table_id = [tenant.bronze, "forge"]
    stock = tenant.pipeline.namespace()
    lance.write_dataset(_rows(range(2)), namespace_client=stock, table_id=table_id, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    tenant.tables.append("$".join(table_id))
    described = stock.describe_table(lance_namespace.DescribeTableRequest(id=table_id))
    vended = described.storage_options or {}
    bucket, _, prefix = str(described.location).removeprefix("s3://").partition("/")
    store = boto3.client(
        "s3",
        endpoint_url=vended["endpoint"],
        aws_access_key_id=vended["aws_access_key_id"],
        aws_secret_access_key=vended["aws_secret_access_key"],
        aws_session_token=vended["aws_session_token"],
        region_name=vended.get("region", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
    listed = store.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/_versions/")
    # The write tier's own allowance, so the refusal below is that tier's Deny on `_refs/`, not a read-only credential.
    store.put_object(Bucket=bucket, Key=f"{prefix}/data/forge-control-{uuid.uuid4().hex}.bin", Body=b"control")

    with pytest.raises(ClientError) as refused:
        store.put_object(Bucket=bucket, Key=f"{prefix}/_refs/tags/published.json", Body=json.dumps({"version": 1, "branch": None}).encode())

    assert listed["KeyCount"] >= 1, "the vended credential could not read the table it was vended for"
    assert refused.value.response["Error"]["Code"] == "AccessDenied", refused.value.response
    assert "published" not in lance.dataset(namespace_client=stock, table_id=table_id).tags.list()
