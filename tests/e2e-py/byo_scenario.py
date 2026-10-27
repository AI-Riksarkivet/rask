"""The bring-your-own pipeline scenario Phase 1's acceptance drives, once per session ([[LH-478]], XC-090).

The lakehouse runs no pipeline. A user's job, on its own compute, writes every tier through the catalog's Lance
Namespace doors with a stock client, follows the commit feed to learn what to do next, and publishes through the
tag gate. `drive` is that job, run from outside the cluster:

1. it seeds bronze with stock pylance through the namespace, on the credential the catalog vends, each commit staging
   the run that made it as `rask.*` transaction properties;
2. it follows lineage's `GET /events?since=` forward to the bronze write and the version it committed;
3. it derives silver through the catalog's create and insert doors, naming its run and the bronze input at that
   version in the spec Context (`x-rask-run-id`, `x-rask-job`, `x-rask-inputs`);
4. it publishes: the project's gate holds a breaching version behind `published`, readers of `@published` keep the
   previous version, a validator approves while the admin watches the project, and the admin stops watching once
   told;
5. it follows the feed over silver and reads its writes and both publications in commit order;
6. it writes a branch, as every table writer may (D3): it cuts `staging` from silver and commits a stock append on it
   through `version/create`, naming the run and the bronze input as transaction properties.

Three Dex users from the deployed configmap play the parts. The estate admin creates a scratch tenant (a project,
a warehouse with its own bucket, a bronze and a silver namespace), declares its gate and watches it. The pipeline
identity holds only `writer` on the two namespaces, so it owns what it creates. The validator holds `validator` on
silver. The tenant is deleted at the end of the session, unless the session keeps the drive for the soak (`kept_drive`).

ONE DRIVE, MANY READERS. `tests/e2e-py/conftest.py` serves the tenant and one drive's `ScenarioRun` as session
fixtures, so each module that asserts on the scenario reads the same run: the scenario's own claims
(`test_a_bring_your_own_pipeline_runs_end_to_end.py`) and every `phase1` criterion module. The run records what the
pipeline did and saw while it ran, each commit with the version its write answered and the run it named, so a reader
asserts it afterwards without driving again.
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import lance
import lance_namespace
import pyarrow as pa
import pyarrow.ipc as ipc
import requests
from principals import DEX_CLIENT, DEX_SCOPE, principal_of
from pydantic import BaseModel, Field


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "").rstrip("/")
#: The notifications service under its API prefix (`http://<host>:8850/api`).
NOTIFICATIONS = os.environ.get("LANCE_E2E_NOTIFICATIONS_URL", "").rstrip("/")
DEX = os.environ.get("LANCE_E2E_DEX", "").rstrip("/")
DEX_SECRET = os.environ.get("LANCE_E2E_DEX_SECRET", "lance-catalog-secret")
ADMIN_USER = os.environ.get("LANCE_E2E_USER", "alice@example.com")
PIPELINE_USER = os.environ.get("LANCE_E2E_PIPELINE_USER", "publisher@rask.internal")
VALIDATOR_USER = os.environ.get("LANCE_E2E_VALIDATOR_USER", "bob@example.com")
#: How long a committed write may take to reach lineage's feed and an inbox.
SETTLE_SECONDS = float(os.environ.get("LANCE_E2E_SETTLE_SECONDS", "120"))
ARROW = "application/vnd.apache.arrow.stream"
#: The branch the pipeline cuts from silver and commits on.
STAGING = "staging"
#: Every request a suite's client sends closes its connection, so no pooled connection is kept to go stale. Through
#: `kubectl port-forward` a server's close of an idle keep-alive connection reaches the client only as its next request
#: fails, measured on the e2e-stack lane: `RemoteDisconnected` on a GET (run 39015745966), and hyper's `IncompleteMessage`
#: on pylance's own POSTs and describes once a stock client sat idle past the catalog's five-second keep-alive (runs
#: 39029359505, 39029400893, 39029401980, 2026-10-17). A POST must not be retried (RFC 9112 § 9.3.1), so the connection
#: is not reused at all.
CLOSE_EACH_REQUEST = "close"

#: Whether a deployed estate is named, and what to set when it is not.
LIVE = bool(CATALOG and LINEAGE and NOTIFICATIONS and DEX)
REQUIRES = "set LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL, LANCE_E2E_NOTIFICATIONS_URL and LANCE_E2E_DEX (a deployed estate)"


class Identity:
    """One caller of the estate's doors: its bearer, and the principal the estate's grants name ([[LH-063]])."""

    def __init__(self, bearer: str) -> None:
        self.bearer = bearer
        self.principal = principal_of(CATALOG, bearer)
        self.http = requests.Session()
        self.http.headers["authorization"] = f"Bearer {bearer}"
        self.http.headers["connection"] = CLOSE_EACH_REQUEST

    @classmethod
    def person(cls, email: str) -> Identity:
        """A Dex user, signed in with the password grant."""
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
        return cls(str(answer.json()["id_token"]))

    def namespace(self, **context: str) -> lance_namespace.LanceNamespace:
        """The stock client an outside pipeline gets, sending ``context`` as spec Context headers on every call."""
        headers = {f"headers.{name}": value for name, value in context.items()}
        return lance_namespace.connect(
            "rest", {"uri": CATALOG, "headers.Authorization": f"Bearer {self.bearer}", "headers.Connection": CLOSE_EACH_REQUEST, **headers}
        )

    def inbox(self, *, since: datetime) -> list[dict[str, Any]]:
        """This person's inbox rows about things that happened at or after ``since``, newest first.

        The inbox pages newest first by `occurred_at`, so paging stops at the first page that reaches back past
        ``since``: an estate's long-lived inbox is not read whole on every poll.
        """
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 100, **({"cursor": cursor} if cursor else {})}
            page = ok(self.http.get(f"{NOTIFICATIONS}/notifications/inbox", params=params, timeout=30))
            rows += page["notifications"]
            cursor = page.get("next_cursor")
            if not cursor or not page["notifications"] or datetime.fromisoformat(page["notifications"][-1]["occurred_at"]) < since:
                return [row for row in rows if datetime.fromisoformat(row["occurred_at"]) >= since]


def ok(answer: requests.Response) -> dict[str, Any]:
    assert answer.status_code == 200, f"{answer.request.method} {answer.request.url} -> {answer.status_code}: {answer.text[:500]}"
    return answer.json()


def rows_of(ids: range) -> pa.Table:
    return pa.table({"id": pa.array(list(ids), pa.int64()), "payload": pa.array([f"row-{i}" for i in ids])})


def arrow_ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


class Tenant:
    """The scratch tenant one session drives, and what it must take down again."""

    def __init__(self, project: str | None = None) -> None:
        """A tenant of its own, or the one named ``project`` that an earlier session made, signed in again."""
        self.admin = Identity.person(ADMIN_USER)
        self.pipeline = Identity.person(PIPELINE_USER)
        self.validator = Identity.person(VALIDATOR_USER)
        self.project = project or f"byo-{uuid.uuid4().hex[:8]}"
        self.bronze = f"{self.project}-bronze"
        self.silver = f"{self.project}-silver"
        self.tables: list[str] = []
        self.watching = False
        #: The wall clock just before the tenant's first door, which `create` sets: the tenant's grants are told from
        #: here, before any write, so a reader of the inboxes or the bus reads from here.
        self.created_at = datetime.now(UTC)
        #: Whether the session keeps the tenant past its end, for the soak to read (`kept_drive.keep`).
        self.kept = False

    @property
    def people(self) -> dict[str, Identity]:
        """The three people the scenario names, by the part each plays."""
        return {"admin": self.admin, "pipeline": self.pipeline, "validator": self.validator}

    def create(self) -> None:
        admin = self.admin
        self.created_at = datetime.now(UTC)
        ok(admin.http.post(f"{CATALOG}/management/v1/projects", json={"id": self.project}))
        ok(admin.http.post(f"{CATALOG}/management/v1/warehouses", json={"id": self.project, "project": self.project}))
        for namespace, tier in ((self.bronze, "bronze"), (self.silver, "silver")):
            body = {"namespace": namespace, "properties": {"rask.tier": tier}}
            ok(admin.http.post(f"{CATALOG}/management/v1/warehouses/{self.project}/namespaces", json=body))
            ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{namespace}/access/grant", json={"user": self.pipeline.principal, "relation": "writer"}))
        ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{self.silver}/access/grant", json={"user": self.validator.principal, "relation": "validator"}))
        self.declare_gate(review=False)

    def declare_gate(self, *, review: bool) -> None:
        body = {"key_column": "id", "review_band": 0.25, "review_enabled": review}
        ok(self.admin.http.post(f"{CATALOG}/management/v1/project/{self.project}/gate/set", json=body))

    def watch(self) -> None:
        ok(self.admin.http.put(f"{NOTIFICATIONS}/notifications/watches/{self.project}", timeout=30))
        self.watching = True

    def unwatch(self) -> None:
        ok(self.admin.http.delete(f"{NOTIFICATIONS}/notifications/watches/{self.project}", timeout=30))
        self.watching = False

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


def lance_facet(row: dict[str, Any]) -> dict[str, Any]:
    """The `rask_lance` facet of a feed row: on the run of a write, on the dataset of a definition change."""
    event = row.get("event") or {}
    carrier = event.get("run") or event.get("dataset") or {}
    return (carrier.get("facets") or {}).get("rask_lance") or {}


def feed_start(identity: Identity) -> int:
    """Where a pipeline that has read nothing yet starts following: the newest row it may see, else the oldest held."""
    body = ok(identity.http.get(f"{LINEAGE}/events", params={"limit": 1}, timeout=30))
    events = body.get("events") or []
    return int(events[0]["seq"]) if events else max(int(body.get("oldest_seq") or 1) - 1, 0)


def follow(identity: Identity, *, since: int, table: str, until: Callable[[list[dict[str, Any]]], bool]) -> tuple[list[dict[str, Any]], int]:
    """Follow the feed forward from ``since`` the way the contract says (`docs/COMMIT-EVENTS.md`): pass `next_cursor`
    back as `since` every time, until the rows read so far satisfy ``until``. The rows, and the cursor to resume from."""
    rows: list[dict[str, Any]] = []
    cursor = since
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        body = ok(identity.http.get(f"{LINEAGE}/events", params={"since": cursor, "table": table, "limit": 500}, timeout=30))
        rows += body["events"]
        assert int(body["next_cursor"]) >= cursor, f"the cursor moved backwards: {cursor} -> {body['next_cursor']}"
        cursor = int(body["next_cursor"])
        if until(rows):
            return rows, cursor
        assert time.monotonic() < deadline, (
            f"the feed for {table} did not show what was expected within {SETTLE_SECONDS:.0f} s: {[lance_facet(r) for r in rows]}"
        )
        time.sleep(2)


def read_forward(identity: Identity, *, since: int, table: str) -> list[dict[str, Any]]:
    """Every row the feed holds after ``since`` that names ``table``, read forward until a poll answers its own cursor."""
    rows: list[dict[str, Any]] = []
    cursor = since
    while True:
        body = ok(identity.http.get(f"{LINEAGE}/events", params={"since": cursor, "table": table, "limit": 500}, timeout=30))
        rows += body["events"]
        assert int(body["next_cursor"]) >= cursor, f"the cursor moved backwards: {cursor} -> {body['next_cursor']}"
        if not body["events"] and int(body["next_cursor"]) == cursor:
            return rows
        cursor = int(body["next_cursor"])


def writes(rows: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """``(operation, version)`` of each row that names a committed version, in feed order."""
    return [(str(lance_facet(row).get("operation")), int(lance_facet(row)["version"])) for row in rows if "version" in lance_facet(row)]


def published_rows(identity: Identity, table_id: list[str]) -> int:
    """What a stock reader of `@published` sees: the row count of the version the tag names."""
    return lance.dataset(namespace_client=identity.namespace(), table_id=table_id, version="published").count_rows()


class NamedRun(BaseModel):
    """The run a write names, as the pipeline names it: its id, its job, and each input as a table id and the version
    it was read at. Bronze's source is outside the catalog, so its runs name no input."""

    run_id: str
    job: str
    inputs: list[dict[str, Any]] = Field(default_factory=list)

    def context(self) -> dict[str, str]:
        """The spec Context headers that name this run on a catalog door."""
        return {"x-rask-run-id": self.run_id, "x-rask-job": self.job, "x-rask-inputs": json.dumps(self.inputs)}

    def transaction_properties(self, *, author: str) -> dict[str, str]:
        """The `rask.*` transaction properties a stock client stages for this run when ``author`` commits it
        (`catalog.core.run_context`): `rask.inputs` only when the run names an input."""
        stamped = {"rask.run_id": self.run_id, "rask.job": self.job, "rask.author": author}
        return stamped | ({"rask.inputs": json.dumps(self.inputs)} if self.inputs else {})


class Commit(BaseModel):
    """One commit the pipeline made: the table, the ref it landed on, the version its write answered there, and the
    run it named."""

    table: str
    #: The branch the commit landed on; ``None`` is main.
    branch: str | None = None
    version: int
    run: NamedRun
    #: The lineage operation the catalog door that made the commit stamps as `rask.operation`; ``None`` for a stock
    #: commit through version/create, which carries only what its writer staged.
    operation: str | None = None


class ScenarioRun(BaseModel):
    """What one drive did and what the pipeline saw while it ran, in the order it happened."""

    #: The feed cursor the pipeline started following from, before its first write.
    start: int
    bronze_table: str
    silver_table: str
    #: Every commit the pipeline made, in order: bronze's stock seed, silver's create and insert, and its append on `staging`.
    commits: list[Commit]
    #: The publish door's answers: the first version, then the breaching one the gate holds.
    first_publish: dict[str, Any]
    held_publish: dict[str, Any]
    #: Rows a stock reader of `@published` saw, and rows a stock reader of main saw, while the breach was held.
    published_rows_while_held: int
    stock_rows_while_held: int
    approval: dict[str, Any]
    published_rows_after_approval: int
    #: Silver's feed rows through the approval's publication, the cursor after them, and a poll from that cursor.
    silver_rows: list[dict[str, Any]]
    cursor: int
    caught_up: dict[str, Any]


def drive(tenant: Tenant) -> ScenarioRun:
    """Run the pipeline once on ``tenant`` and record what it saw (the module docstring's five steps)."""
    pipeline, validator, admin = tenant.pipeline, tenant.validator, tenant.admin
    bronze_id, silver_id = [tenant.bronze, "orders"], [tenant.silver, "orders"]
    bronze_table, silver_table = "$".join(bronze_id), "$".join(silver_id)
    started_at = datetime.now(UTC)
    start = feed_start(pipeline)

    # 1. Seed bronze with stock pylance through the namespace, each commit naming its run: a first load, then an increment.
    stock = pipeline.namespace()
    seed_run = NamedRun(run_id="byo-seed-1", job="byo-bronze")
    seeded = lance.write_dataset(
        rows_of(range(4)),
        namespace_client=stock,
        table_id=bronze_id,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        transaction_properties=seed_run.transaction_properties(author=pipeline.principal),
    )
    tenant.tables.append(bronze_table)
    increment_run = seed_run.model_copy(update={"run_id": "byo-seed-2"})
    increment = lance.write_dataset(
        rows_of(range(4, 8)),
        namespace_client=stock,
        table_id=bronze_id,
        mode="append",
        transaction_properties=increment_run.transaction_properties(author=pipeline.principal),
    )
    commits = [Commit(table=bronze_table, version=seeded.version, run=seed_run), Commit(table=bronze_table, version=increment.version, run=increment_run)]

    # 2. Follow the feed to the bronze write: its committed version is the input silver derives from.
    bronze_rows, _ = follow(pipeline, since=start, table=bronze_table, until=lambda rows: ("insert", 2) in writes(rows))
    bronze_version = max(version for operation, version in writes(bronze_rows) if operation == "insert")

    # 3. Derive silver through the catalog's doors, naming the run and the input in the spec Context.
    first_run = NamedRun(run_id="byo-run-1", job="byo-silver", inputs=[{"table": bronze_id, "version": bronze_version}])
    silver_start = feed_start(pipeline)
    created = pipeline.namespace(**first_run.context()).create_table(
        lance_namespace.CreateTableRequest(id=silver_id, mode="create"), arrow_ipc(rows_of(range(8)))
    )
    tenant.tables.append(silver_table)
    first = ok(pipeline.http.post(f"{CATALOG}/management/v1/table/{silver_table}/publish", json={"version": 1, "key_column": "id"}, timeout=120))

    # 4. The project turns review on; the next batch triples silver, past the 0.25 band, and is held behind `published`.
    tenant.declare_gate(review=True)
    second_run = first_run.model_copy(update={"run_id": "byo-run-2"})
    inserted = pipeline.namespace(**second_run.context()).insert_into_table(
        lance_namespace.InsertIntoTableRequest(id=silver_id, mode="append"), arrow_ipc(rows_of(range(8, 24)))
    )
    assert created.version is not None and inserted.version is not None, f"silver's create answered version {created.version}, its insert {inserted.version}"
    commits += [
        Commit(table=silver_table, version=created.version, run=first_run, operation="create_table"),
        Commit(table=silver_table, version=inserted.version, run=second_run, operation="insert"),
    ]
    held = ok(pipeline.http.post(f"{CATALOG}/management/v1/table/{silver_table}/publish", json={"version": 2, "key_column": "id"}, timeout=120))
    published_while_held = published_rows(admin, silver_id)
    stock_while_held = lance.dataset(namespace_client=stock, table_id=silver_id).count_rows()

    # The validator approves; the admin, watching from just before, is told by the approval's announcement.
    tenant.watch()
    approved = ok(validator.http.post(f"{CATALOG}/management/v1/table/{silver_table}/promotion/approve", json={"version": 2}, timeout=120))
    published_after = published_rows(admin, silver_id)

    # 5. Following silver's feed reads its writes and both publications in commit order.
    silver_rows, cursor = follow(pipeline, since=silver_start, table=silver_table, until=lambda rows: ("publish_table", 2) in writes(rows))
    caught_up = ok(pipeline.http.get(f"{LINEAGE}/events", params={"since": cursor, "table": silver_table}, timeout=30))

    # The watcher stops watching once the approval reached it, so a later delivery of an earlier run reaches nobody
    # new. Whether it reached it, and nothing else, is criterion 4's targeting claim (`test_events_are_correct.py`).
    announced = [row for row in silver_rows if writes([row]) == [("publish_table", 2)]]
    notification = f"{announced[0]['event']['run']['runId']}@COMPLETE" if announced else ""
    deadline = time.monotonic() + SETTLE_SECONDS
    while notification and time.monotonic() < deadline and not any(row["notification_id"] == notification for row in admin.inbox(since=started_at)):
        time.sleep(3)
    tenant.unwatch()

    # 6. The pipeline cuts `staging` from silver through the spec's CreateTableBranch, then commits a stock append on it
    #    through version/create, naming its run and the bronze input as transaction properties.
    pipeline.namespace().create_table_branch(lance_namespace.CreateTableBranchRequest(id=silver_id, name=STAGING))
    branch_run = first_run.model_copy(update={"run_id": "byo-run-3"})
    on_branch = lance.write_dataset(
        rows_of(range(24, 28)),
        lance.dataset(namespace_client=stock, table_id=silver_id).checkout_version((STAGING, None)),
        mode="append",
        transaction_properties=branch_run.transaction_properties(author=pipeline.principal),
    )
    commits.append(Commit(table=silver_table, branch=STAGING, version=on_branch.version, run=branch_run))

    return ScenarioRun(
        start=start,
        bronze_table=bronze_table,
        silver_table=silver_table,
        commits=commits,
        first_publish=first,
        held_publish=held,
        published_rows_while_held=published_while_held,
        stock_rows_while_held=stock_while_held,
        approval=approved,
        published_rows_after_approval=published_after,
        silver_rows=silver_rows,
        cursor=cursor,
        caught_up=caught_up,
    )
