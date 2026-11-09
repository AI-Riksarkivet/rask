"""Closes-when (a) of [[LH-494]] (ADR 0198): a commit's pass costs a fixed handful of store requests, and an idle estate
costs what is due, never a read of every table.

Counted on the wire, at two estate sizes: the real catalog and the real maintenance app over one moto store, maintenance's
requests passing through `store_requests.counted`, since Lance's and pyarrow's native S3 clients go through no Python
hook. Each table is a catalog table of three commits and three fragments, and a commit's pass is measured after the pass
before that commit.

* **A commit's pass** (`table_pass.run_table_pass`) reads its claim, its trash record, its namespace's warehouse, the rows of
  its bucket's ``__manifest`` naming it and its namespace, the policy records that may govern it and its own datasets,
  each by key: the same number of requests at 4 tables as at 12, and once eight more warehouses and a policy on every
  other table are there, since a pass that listed the registries would read each record. The files its commits recorded are
  checked against one listing of its root, and of the records only the one the commit wrote is read, maintenance's copy
  holding the rest (`lakehouse.file_identities.recorded_and_copied`), so a table of 13 commits reads one. Measured
  2026-11-09: 58. What still grows with a table is Lance's own cleanup accounting, which reads each retained version's
  manifest (`explain_cleanup_old_versions` and `versions`, [[LH-562]]): three reads a version, bounded by the table's
  retention.
* **An idle estate** runs, besides each table's own schedule (its recheck, a day out), only the hourly audit
  (`audit.run_audit`): it opens no table, and grows with the estate by at most one request per table, the claim record its
  drift report reads, which its backstop reuses to find a table whose schedule was lost. Measured: about 1 request per
  table. The orphan scan, which opens every table, is the daily scan's (owner, 2026-11-09). Owner, 2026-11-09, through
  the multi-question tool: an idle table costs about one request an hour and one pass a day, and nothing walks the
  estate's files; a commit's pass costs a fixed handful plus Lance's own cleanup accounting.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterator
from typing import Any, cast

import pyarrow as pa
import pytest
from moto.server import ThreadedMotoServer

from lakehouse import file_identities
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from maintenance.core.config import get_settings as maintenance_settings
from maintenance.services.audit import run_audit
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.table_pass import TableTask, run_table_pass
from tests.integration.catalog_and_maintenance import TwoServices, catalog_and_maintenance
from tests.integration.maintenance_vend_doors import CATALOG_URL, catalog_doors
from tests.integration.store_requests import CountedStore, StoreRequest, counted


_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})
#: The most a pass may cost: what one table's datasets, its records and its bucket's manifest need, with headroom for a
#: pylance that reads a little differently. A pass that read every table would cost a multiple of the estate.
_PASS_CEILING = 80


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


class _IdleQueue:
    """An idle estate's queue: a schedule holds every table but one, whose schedule was lost. `TaskQueue`'s signatures."""

    def __init__(self, scheduled: list[TableTask]) -> None:
        self._scheduled = scheduled
        self.queued: list[Task] = []

    async def held(self, area: str, kind: str) -> set[str]:
        return {table.uuid for table in self._scheduled} if (area, kind) == ("schedule", "table") else set()

    async def enqueue(self, task: Task) -> bool:
        self.queued.append(task)
        return True


class _Estate:
    """One warehouse's bronze namespace, grown a table at a time through the catalog's doors."""

    def __init__(self, both: TwoServices, store: CountedStore) -> None:
        self.both = both
        self.store = store
        self.tables: list[TableTask] = []
        ok = both.catalog.post("/management/v1/projects", json={"id": "acme"})
        assert ok.status_code == 200, ok.text
        warehouse = f"wh-{uuid.uuid4().hex[:6]}"
        for path, body in (
            ("/management/v1/warehouses", {"id": warehouse, "project": "acme"}),
            (f"/management/v1/warehouses/{warehouse}/namespaces", {"namespace": "bronze"}),
        ):
            answer = both.catalog.post(path, json=body)
            assert answer.status_code == 200, answer.text

    def grow(self, to: int) -> None:
        """Tables made until there are ``to``, each of three commits."""
        while len(self.tables) < to:
            table = f"bronze$t{len(self.tables)}"
            for door in ("create", "insert", "insert"):
                self._commit(table, door)
            described = self.both.catalog.post(f"/v1/table/{table}/describe", json={}).json()
            self.tables.append(TableTask(uuid=str(described["properties"]["rask.uuid"]), location=str(described["location"])))

    def crowd(self, warehouses: int, *, spared: set[int]) -> None:
        """``warehouses`` more warehouses of the project, and a policy set on every table but those ``spared``."""
        for _ in range(warehouses):
            answer = self.both.catalog.post("/management/v1/warehouses", json={"id": f"wh-{uuid.uuid4().hex[:6]}", "project": "acme"})
            assert answer.status_code == 200, answer.text
        for index in set(range(len(self.tables))) - spared:
            answer = self.both.catalog.post(f"/management/v1/table/bronze$t{index}/policy/set", json={"retention_days": 30})
            assert answer.status_code == 200, answer.text

    def deepen(self, table: int, commits: int) -> None:
        """The ``table``-th table given ``commits`` more commits."""
        for _ in range(commits):
            self._commit(f"bronze$t{table}", "insert")

    def a_commits_pass(self, table: int) -> list[StoreRequest]:
        """What the ``table``-th table's pass asked the store for one more commit, its pass before that commit already run."""
        self._pass(table)
        self._commit(f"bronze$t{table}", "insert")
        self.store.taken()
        self._pass(table)
        return self.store.taken()

    def records_read(self, seen: list[StoreRequest], table: int) -> int:
        """How many of the ``table``-th table's commit records the requests ``seen`` read."""
        held = f"/{file_identities.records_of(self.tables[table].uuid)}/"
        return sum(1 for request in seen if request.method == "GET" and held in request.path)

    def _commit(self, table: str, door: str) -> None:
        answer = self.both.catalog.post(f"/v1/table/{table}/{door}", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert answer.status_code == 200, answer.text

    def _pass(self, table: int) -> None:
        with catalog_doors(self.both.catalog):
            outcome = run_table_pass(self.tables[table], maintenance_settings())
        assert [result.error for result in outcome.results] == [None], outcome

    def audited(self) -> list[StoreRequest]:
        """What one hourly audit of the idle estate asked the store, which queues the pass of the one table whose schedule
        was lost, at the location its announcement would name."""
        *scheduled, lost = self.tables
        queue = _IdleQueue(scheduled)
        self.store.taken()
        portal = self.both.maintenance.portal
        assert portal is not None, "the maintenance client was never entered"
        with catalog_doors(self.both.catalog):
            out: dict[str, Any] = portal.call(lambda: run_audit(maintenance_settings(), queue=cast(TaskQueue, queue)))
        assert (out["unscheduled"], queue.queued) == ({"tables": 1, "trash": 0}, [Task(kind="table", uuid=lost.uuid, location=lost.location)])
        return self.store.taken()

    def opened(self, seen: list[StoreRequest]) -> list[StoreRequest]:
        """The requests among ``seen`` that read inside one of the tables."""
        keys = [table.location.removeprefix("s3://").split("/", 1) for table in self.tables]
        return [
            request
            for request in seen
            for bucket, key in keys
            if request.path.startswith(f"/{bucket}/{key}")
            or (request.path.rstrip("/") == f"/{bucket}" and request.query.get("prefix", [""])[0].startswith(key))
        ]


def test_a_commits_pass_costs_a_fixed_handful_and_an_idle_estate_opens_no_table(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with counted(moto_url) as store:
        env = {"MAINTENANCE_CATALOG_URL": CATALOG_URL, "MAINTENANCE_S3_ENDPOINT": store.url, "MAINTENANCE_ORPHAN_SCAN_ENABLED": "true"}
        with catalog_and_maintenance(moto_url, monkeypatch, env=env) as both:
            estate = _Estate(both, store)
            estate.grow(4)
            small_pass, small_audit = estate.a_commits_pass(0), estate.audited()
            estate.grow(12)
            large_pass, large_audit = estate.a_commits_pass(4), estate.audited()
            # A table measured for the first time, as the fifth was: a second pass of the fifth would read one more commit.
            estate.crowd(8, spared={5})
            crowded_pass = estate.a_commits_pass(5)
            estate.deepen(0, 8)
            deep_pass = estate.a_commits_pass(0)

    assert len(large_pass) == len(small_pass) <= _PASS_CEILING, f"a commit's pass cost {len(small_pass)} requests at 4 tables and {len(large_pass)} at 12"
    grown = Counter((request.method, request.path) for request in crowded_pass) - Counter((request.method, request.path) for request in large_pass)
    assert len(crowded_pass) == len(large_pass), f"a commit's pass read {sorted(grown)} beside 8 more warehouses and 10 policies"
    assert estate.records_read(deep_pass, 0) == 1, f"a commit's pass read {estate.records_read(deep_pass, 0)} of its table's 13 commit records"
    assert (estate.opened(small_audit), estate.opened(large_audit)) == ([], []), "the hourly audit read inside a table"
    assert len(large_audit) - len(small_audit) <= 12 - 4, f"the audit grew by {len(large_audit) - len(small_audit)} requests for 8 more tables"
