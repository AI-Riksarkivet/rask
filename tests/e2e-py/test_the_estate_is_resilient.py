"""Criterion 5, the estate resilient, on the shared tenant (XC-095).

Resilience is recovering to a CORRECT state, not only answering again. Each drill takes one dependency of the release
down, drives the doors a pipeline uses while it is gone, brings it back, and ends by holding the estate to the readings
criteria 1 and 4 hold the scenario to (`correctness`):

- every version of the drill's table is a commit the pipeline made, carrying exactly the run it named and who committed
  it, no write refused while the dependency was gone landed, and no run landed twice;
- the edge list holds exactly one edge for each input those versions name;
- nothing parked on the DLQ stream from the drill's start, and both of the catalog's outboxes drained.

The drills, and what each outage must look like while it lasts:

1. The object store. A write through the catalog's own door is refused ServiceUnavailable (code 17, 503), or gets no
   answer within the bound the catalog's own store timeouts set, and never lands while the store is gone.
2. OpenFGA, the authorization decision. A governed write answers ServiceUnavailable at its authorization check: never an
   allow, and never a deny, which would read as a missing grant (`service_kit.governed.fga`).
3. Lineage, the commit feed's recorder. Commits go on landing, and each announcement is recorded once lineage is back:
   its durable consumer resumes where it stopped.
4. The bus, held down past the window a sidecar's NATS client reconnects for. Commits and grants go on landing while
   NATS is gone, each announcement staged in the catalog's outbox, and the lineage relay records the staged writes
   without the bus. The outage closes every sidecar's connection for good, so each service restarts its own sidecar once
   the bus answers again (`service_kit.bus_watchdog`), and both relays publish what they hold.
5. The issuer. A door verifies a bearer offline against the issuer's key set, which it holds for 300 s
   (`service_kit.governed.oidc`): an existing token is accepted while the door holds the keys, answered
   ServiceUnavailable once a door must fetch them and cannot (never Unauthenticated, the caller is not at fault), and
   accepted again when the issuer is back, with no restart.
6. The secret store, sealed in place. The catalog drops its signing key once it can no longer re-read it from the store,
   and then refuses every write it would announce, ServiceUnavailable, before committing it: a write it cannot announce
   never lands. The store's contents survive the seal, so nothing is minted again.
7. The catalog itself, its process killed with SIGKILL while pipelines commit concurrently through `version/create`.
   Lance's put-if-not-exists of each `_versions/` manifest (`lance_docs/file_format.md` § Commit Protocol) keeps one
   history: every acknowledged commit landed once, a commit whose answer the kill lost landed at most once, and no run
   landed twice.

THE DRILL TABLES live in a namespace the module makes in the shared tenant's warehouse, under a name that does not carry
the tenant's project id, as criterion 2's matrix does, so criteria 1 and 4 read only what the scenario made. The pipeline
holds `writer` there and owns each table it makes. Every commit names a run whose input is the namespace's source table
at version 1, so every version has an edge to hold the edge list to.

A DRILL BEGINS QUIET. Before it takes anything down it waits until every service that acts on a lane has acknowledged
every message the bus holds, so the outage meets no delivery in flight, and its DLQ window opens there. Its outage ends
in a `finally`, so a drill that fails hands the release back with what it took down serving again.

WHAT IS NOT CLAIMED, and why. A commit whose writer never got its answer may lack its announcement: a kill between the
Lance commit and the outbox stage loses it (LH-497, #641, parked; ADR 0037), so such a version is held to at most one
edge, never two. The service-account issuer is the cluster's API server, which no drill can take down without the
cluster. A secret held by a running pod expires 300 to 600 s after the store stops confirming it (ADR 0166); the seal
outlasts the signing key's re-read, not every secret's, and the expiry of the rest is pinned offline.

A drill runs only when its run asks for it with `--chaos` (`conftest.py`): the e2e-stack lane does on every push, against
its throwaway cluster, and the deployed release only when invoked explicitly, with `bash scripts/e2e_live.sh -m phase1
--require-live --chaos`.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Literal

import bus
import byo_scenario as scenario
import chaos
import lance
import lance_namespace as ln
import pytest
import requests
from byo_scenario import CATALOG, Commit, NamedRun, Tenant
from chaos import Workload
from correctness import as_named, edges_named, history_of, rask_properties, recorded_edges, settled_control_outbox_depth, settled_outbox_depth
from pydantic import BaseModel


#: The durable consumers that act on each lane: lineage records the feed, notifications fills the inboxes.
LINEAGE_DURABLES = ("lineage-durable", "notifications-durable")
CONTROL_DURABLES = ("notifications-control-durable",)
#: The spec's code for a door that cannot answer now (`lance_docs/ns_catalog/spec.yaml`, `ErrorResponse.code`).
SERVICE_UNAVAILABLE = 17
#: How long an announcement may take to land once its dependency is back: a delivery its sidecar retries waits out a
#: 120 s step (`chart/templates/dapr-resiliency.yaml`), and a relay ticks every 30 s (lineage) or 60 s (control).
RECOVERY_SECONDS = chaos.SETTLE_SECONDS
#: How long the bus stays down: past the window a sidecar's NATS client goes on reconnecting for, since a client that
#: is still reconnecting buffers its publishes and sends them once the bus is back, so a shorter outage loses nothing
#: whether the outbox is on or off and asks nothing of the estate's recovery.
BUS_OUTAGE_SECONDS = chaos.RECONNECT_WINDOW_SECONDS + 30.0
#: What a stock client says of a commit whose answer it lost, which may have landed, and of a request no door answered,
#: which committed nothing (measured on pylance 13.0.0 with the catalog killed under concurrent appends).
COMMIT_RESULT_UNKNOWN = "the commit may or may not have been applied"
NO_DOOR = "Failed to execute request"
#: What a stock client says of a commit Lance gave up on after rebasing it over its concurrent writers' commits, which
#: committed nothing (`lance_docs/file_format.md` § Commit Algorithm). The kill drill's writers resume together once the
#: catalog is back, and on the e2e-stack lane's runner one exhausted its retries (run 39063208675: "Too many concurrent
#: writers. Attempted 5 times, but failed on retry_timeout of 30.000 seconds.").
CONTENDED = "Too many concurrent writers"
#: How many kills the kill drill may spend before one meets a commit in flight: a kill can land between two commits.
KILLS = 3
ARROW = "application/vnd.apache.arrow.stream"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.phase1,
    pytest.mark.chaos,
    pytest.mark.skipif(not (scenario.LIVE and bus.NATS), reason=f"{scenario.REQUIRES}, and LANCE_E2E_NATS_URL (the bus)"),
]


class Refusal(BaseModel):
    """A write a door did not take: the spec code its answer carried, or none when it never reached a door."""

    code: int | None
    text: str


class Drills:
    """The drills' namespace on the shared tenant, its source table, and what the pipeline did there: every commit it
    was told landed, and every run whose write it never got an answer for."""

    def __init__(self, tenant: Tenant) -> None:
        self.tenant = tenant
        self.pipeline = tenant.pipeline
        self.namespace = f"c5-{uuid.uuid4().hex[:8]}"
        self.source = [self.namespace, "src"]
        self.commits: list[Commit] = []
        self.unanswered: list[NamedRun] = []
        self._lock = threading.Lock()

    def build(self) -> None:
        """Make the namespace, grant the pipeline `writer` on it, and have the pipeline make the source table."""
        admin = self.tenant.admin
        body = {"namespace": self.namespace, "properties": {"rask.tier": "bronze"}}
        scenario.ok(admin.http.post(f"{CATALOG}/management/v1/warehouses/{self.tenant.project}/namespaces", json=body, timeout=60))
        self.grant(self.pipeline.principal, "writer")
        self._commit(self.source, range(4), NamedRun(run_id=f"c5-src-{uuid.uuid4().hex[:6]}", job="c5-source"), mode="create")

    def grant(self, principal: str, relation: str) -> None:
        """The tenant's admin grants ``relation`` on the namespace, which the catalog announces on the control lane."""
        grant = {"user": principal, "relation": relation}
        scenario.ok(self.tenant.admin.http.post(f"{CATALOG}/management/v1/namespace/{self.namespace}/access/grant", json=grant, timeout=60))

    def run(self, table: list[str]) -> NamedRun:
        """A fresh run of the pipeline's job for ``table``, reading the source table at version 1."""
        return NamedRun(run_id=f"c5-{table[-1]}-{uuid.uuid4().hex[:8]}", job=f"c5-{table[-1]}", inputs=[{"table": self.source, "version": 1}])

    def make(self, name: str) -> list[str]:
        """A table the pipeline makes in the namespace with a stock client, its first commit naming a run."""
        table = [self.namespace, name]
        self._commit(table, range(4), self.run(table), mode="create")
        return table

    def append(self, table: list[str], ids: range) -> Commit:
        """A stock client's append, staged on the credential the catalog vends and committed through `version/create`."""
        return self._commit(table, ids, self.run(table), mode="append")

    def try_append(self, table: list[str], ids: range) -> Commit | None:
        """A stock append that may fail the way an outage fails it: the commit, or ``None`` when no door took it or its
        answer was lost, the second with its run recorded as unanswered, since it may still have landed. Any other
        failure raises: a door's refusal or a client's error is not what an outage looks like."""
        run = self.run(table)
        try:
            return self._commit(table, ids, run, mode="append")
        except (ln.LanceNamespaceError, OSError) as error:
            failure = _outage_failure(error)
            if failure is None:
                raise
            if failure == "lost":
                with self._lock:
                    self.unanswered.append(run)
            return None

    def insert(self, table: list[str], ids: range, *, within: float) -> Commit | Refusal:
        """An insert through the catalog's own door, the run named in the spec Context: the commit, or the refusal. No
        answer within ``within`` is a refusal too, and its run is recorded as unanswered: a door that never answered may
        still commit."""
        run = self.run(table)
        try:
            answer = self.pipeline.http.post(
                f"{CATALOG}/v1/table/{'$'.join(table)}/insert",
                params={"mode": "append"},
                data=scenario.arrow_ipc(scenario.rows_of(ids)),
                headers={"content-type": ARROW, **run.context()},
                timeout=within,
            )
        except requests.Timeout:
            with self._lock:
                self.unanswered.append(run)
            return Refusal(code=None, text=f"no answer within {within:.0f} s")
        except requests.ConnectionError as error:
            return Refusal(code=None, text=f"no door reached: {str(error)[:200]}")
        if answer.status_code != 200:
            return Refusal(code=_problem_code(answer), text=f"{answer.status_code}: {answer.text[:300]}")
        return self._record(table, int(answer.json()["version"]), run)

    def describe(self, table: list[str]) -> int | Refusal:
        """The version the catalog's DescribeTable reports for ``table`` as the pipeline, or the refusal. The version is
        filled only when the request asks for detailed metadata."""
        try:
            described = self.pipeline.namespace().describe_table(ln.DescribeTableRequest(id=table, vend_credentials=False, load_detailed_metadata=True))
        except ln.LanceNamespaceError as error:
            return Refusal(code=error.code, text=str(error)[:300])
        assert described.version is not None, f"{table} described no version"
        return int(described.version)

    def describe_at_the_door(self, table: list[str]) -> int | Refusal:
        """DescribeTable sent raw, so a refusal carries the door's own code: pylance's client answers code 17 for a host it
        cannot reach as well, and the issuer drill must tell the door's verdict from no door at all."""
        try:
            answer = self.pipeline.http.post(
                f"{CATALOG}/v1/table/{'$'.join(table)}/describe", json={"vend_credentials": False, "load_detailed_metadata": True}, timeout=60
            )
        except requests.ConnectionError as error:
            return Refusal(code=None, text=f"no door reached: {str(error)[:200]}")
        if answer.status_code != 200:
            return Refusal(code=_problem_code(answer), text=f"{answer.status_code}: {answer.text[:300]}")
        return int(answer.json()["version"])

    def serving(self, table: list[str]) -> None:
        """Wait until the catalog describes ``table`` to the pipeline again."""
        chaos.until(lambda: isinstance(self.describe(table), int), f"the catalog did not describe {table} again", within=RECOVERY_SECONDS)

    def _commit(self, table: list[str], ids: range, run: NamedRun, *, mode: Literal["create", "append"]) -> Commit:
        stamped = run.transaction_properties(author=self.pipeline.principal)
        client = self.pipeline.namespace()
        if mode == "create":
            written = lance.write_dataset(
                scenario.rows_of(ids),
                namespace_client=client,
                table_id=table,
                mode="create",
                data_storage_version="2.2",
                enable_stable_row_ids=True,
                transaction_properties=stamped,
            )
        else:
            written = lance.write_dataset(scenario.rows_of(ids), namespace_client=client, table_id=table, mode="append", transaction_properties=stamped)
        return self._record(table, written.version, run)

    def _record(self, table: list[str], version: int, run: NamedRun) -> Commit:
        commit = Commit(table="$".join(table), version=version, run=run)
        with self._lock:
            self.commits.append(commit)
        return commit

    def quiet(self) -> datetime:
        """Wait until every service that acts on a lane has acknowledged every message the bus holds; the instant the
        drill's DLQ window opens."""
        bus.wait_until_acked("LINEAGE", LINEAGE_DURABLES, bus.last_seq("LINEAGE"), within=RECOVERY_SECONDS)
        bus.wait_until_acked("CATALOG_CONTROL", CONTROL_DURABLES, bus.last_seq("CATALOG_CONTROL"), within=RECOVERY_SECONDS)
        return datetime.now(UTC)

    def assert_correct(self, tables: Iterable[list[str]], *, since: datetime) -> None:
        """The estate as criteria 1 and 4 read it, over ``tables``.

        Lance holds exactly the commits the pipeline was told landed, plus any whose answer was lost, each at most once;
        the edge list holds one edge for each input an acknowledged commit names, and at most one for a commit whose
        answer was lost (LH-497); nothing parked from ``since``; and both outboxes drained.
        """
        names = ["$".join(table) for table in tables]
        author = self.pipeline.principal
        history = history_of(self.pipeline, names)
        acknowledged = as_named((commit for commit in self.commits if commit.table in names), author=author)
        lost = {run.run_id: rask_properties(run.transaction_properties(author=author)) for run in self.unanswered}
        landed = {key: properties for key, properties in history.items() if key not in acknowledged and properties.get("rask.run_id") in lost}
        runs = Counter(properties.get("rask.run_id") for properties in history.values())
        owed = edges_named({key: history[key] for key in acknowledged if key in history})
        edges = recorded_edges(self.tenant.admin, names, expected=owed, within=RECOVERY_SECONDS)
        unowed = [edge for edge in edges if (edge.out_table, edge.out_branch, edge.out_version) in landed]
        parked = [(message.seq, message.subject) for message in bus.messages_since("DLQ", since)]
        depths = (settled_outbox_depth(self.tenant.admin, within=RECOVERY_SECONDS), settled_control_outbox_depth(within=RECOVERY_SECONDS))

        assert history == acknowledged | landed, "Lance holds a version the pipeline did not commit, or lacks one it was told landed"
        assert all(properties == lost[str(properties["rask.run_id"])] for properties in landed.values())
        assert [run for run, count in runs.items() if count > 1] == []
        assert Counter(edge for edge in edges if edge not in unowed) == Counter(owed)
        assert Counter(unowed) <= Counter(edges_named(landed)), f"a commit whose answer was lost has an edge it does not name, or two: {unowed}"
        assert (parked, depths) == ([], (0, 0))


def _problem_code(answer: requests.Response) -> int | None:
    try:
        body = answer.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def _outage_failure(error: Exception) -> Literal["lost", "refused"] | None:
    """How a stock commit failed because its door was gone or could not answer: its answer lost, or no door took it, or
    Lance gave up on the burst of writers the outage left waiting; or ``None``, a failure no outage explains. The client
    folds all three into its errors' text, so the text is what is read. A refusal committed nothing, and Lance's history
    is the check of that: a refused write that landed is a version no commit was acknowledged for."""
    text = str(error)
    if COMMIT_RESULT_UNKNOWN in text:
        return "lost"
    if isinstance(error, ln.ServiceUnavailableError) or NO_DOOR in text or CONTENDED in text:
        return "refused"
    return None


def _refused(probe: Commit | Refusal | int, *, codes: tuple[int | None, ...] = (SERVICE_UNAVAILABLE,)) -> bool:
    return isinstance(probe, Refusal) and probe.code in codes


@pytest.fixture(scope="module")
def drills(byo_tenant: Tenant) -> Drills:
    made = Drills(byo_tenant)
    made.build()
    return made


def test_an_object_store_outage_refuses_a_write_and_the_estate_recovers_correct(drills: Drills) -> None:
    table = drills.make("store")
    bound = chaos.store_outage_answer_bound()
    started = drills.quiet()

    with chaos.scaled_to_zero(Workload(kind="statefulset", name=chaos.STORE)):
        refused = drills.insert(table, range(4, 8), within=bound)
    chaos.until(chaos.store_answers, "the object store did not answer again", within=RECOVERY_SECONDS)
    drills.serving(table)
    drills.append(table, range(8, 12))

    assert _refused(refused, codes=(SERVICE_UNAVAILABLE, None)), f"a write while the object store was gone answered {refused}"
    drills.assert_correct([table], since=started)


def test_an_authorization_outage_refuses_a_governed_write_and_the_estate_recovers_correct(drills: Drills) -> None:
    table = drills.make("authz")
    started = drills.quiet()

    with chaos.scaled_to_zero(Workload(kind="deployment", name=chaos.OPENFGA)):
        refused = drills.insert(table, range(4, 8), within=RECOVERY_SECONDS)
    drills.serving(table)
    drills.append(table, range(8, 12))

    assert _refused(refused), f"a write while OpenFGA was gone answered {refused}"
    drills.assert_correct([table], since=started)


def test_a_lineage_outage_loses_no_announcement_and_the_estate_recovers_correct(drills: Drills) -> None:
    table = drills.make("lineage")
    started = drills.quiet()

    with chaos.scaled_to_zero(Workload(kind="deployment", name=chaos.LINEAGE)):
        while_gone = [drills.append(table, range(4 * n, 4 * n + 4)) for n in range(1, 4)]
    drills.append(table, range(16, 20))

    assert [commit.version for commit in while_gone] == [2, 3, 4]
    drills.assert_correct([table], since=started)


def test_a_bus_outage_loses_no_announcement_and_the_estate_recovers_correct(drills: Drills) -> None:
    table = drills.make("bus")
    grantee = f"{drills.pipeline.principal.split('~', 1)[0]}~c5-grantee-{uuid.uuid4().hex[:8]}"
    started = drills.quiet()
    sidecars = chaos.sidecar_restarts(chaos.BUS_CLIENTS)

    with chaos.scaled_to_zero(Workload(kind="statefulset", name=chaos.BUS)):
        down_since = time.monotonic()
        while_gone = [drills.append(table, range(4 * n, 4 * n + 4)) for n in range(1, 3)]
        drills.grant(grantee, "reader")
        time.sleep(max(0.0, BUS_OUTAGE_SECONDS - (time.monotonic() - down_since)))
    chaos.until(chaos.bus_answers, "the bus did not answer again", within=RECOVERY_SECONDS)
    chaos.until(lambda: chaos.sidecars_restarted(sidecars), "a sidecar the outage closed was not restarted by its service", within=RECOVERY_SECONDS)
    drills.serving(table)
    drills.append(table, range(12, 16))

    assert [commit.version for commit in while_gone] == [2, 3]
    drills.assert_correct([table], since=started)


def test_an_issuer_outage_keeps_an_existing_token_while_a_door_holds_its_keys_and_refuses_it_once_the_door_must_fetch_them(drills: Drills) -> None:
    catalog = Workload(kind="deployment", name=chaos.CATALOG)
    table = drills.make("issuer")
    chaos.restarted(catalog)
    drills.serving(table)
    started = drills.quiet()

    with chaos.unreachable(chaos.ISSUER):
        held = drills.try_append(table, range(4, 8))
        chaos.restarted(catalog)
        chaos.until(chaos.catalog_answers, "the restarted catalog did not answer", within=RECOVERY_SECONDS)
        fetching = drills.describe_at_the_door(table)
    drills.serving(table)
    drills.append(table, range(8, 12))

    assert isinstance(held, Commit), "an existing token was refused by a door that held the issuer's keys"
    assert _refused(fetching), f"a door that had to fetch the issuer's keys while it was gone answered {fetching}"
    drills.assert_correct([table], since=started)


def test_a_sealed_secret_store_refuses_every_write_the_catalog_cannot_announce_and_the_estate_recovers_correct(drills: Drills) -> None:
    table = drills.make("secrets")
    started = drills.quiet()

    with chaos.sealed_store():
        chaos.until(lambda: not chaos.catalog_signs(), "the catalog still held its signing key with its store sealed", within=chaos.SIGNER_DROP_SECONDS)
        refused = drills.insert(table, range(4, 8), within=RECOVERY_SECONDS)
    chaos.until(chaos.catalog_signs, "the catalog did not sign again once its store was unsealed", within=RECOVERY_SECONDS)
    drills.serving(table)
    drills.append(table, range(8, 12))

    assert _refused(refused, codes=(SERVICE_UNAVAILABLE, None)), f"a write the catalog could not announce answered {refused}"
    drills.assert_correct([table], since=started)


def test_a_catalog_killed_mid_commit_keeps_one_history_and_the_estate_recovers_correct(drills: Drills) -> None:
    catalog = Workload(kind="deployment", name=chaos.CATALOG)
    table = drills.make("kill")
    named = "$".join(table)
    started = drills.quiet()
    stop = threading.Event()
    flying = [0]
    failed: list[Exception] = []
    lock = threading.Lock()

    def landed() -> int:
        return sum(1 for commit in drills.commits if commit.table == named)

    def lost() -> list[str]:
        return [run.run_id for run in drills.unanswered if run.job == f"c5-{table[-1]}"]

    def writing(holds: Callable[[], bool]) -> Callable[[], bool]:
        """``holds``, unless a writer failed the way no outage explains, which then fails the drill with its own error."""

        def check() -> bool:
            if failed:
                raise failed[0]
            return holds()

        return check

    def writer() -> None:
        while not stop.is_set():
            with lock:
                flying[0] += 1
            try:
                made = drills.try_append(table, range(1))
            except Exception as error:  # noqa: BLE001 — handed to the drill's own thread, which raises it
                failed.append(error)
                return
            finally:
                with lock:
                    flying[0] -= 1
            if made is None:
                time.sleep(0.5)

    writers = [threading.Thread(target=writer) for _ in range(4)]
    for thread in writers:
        thread.start()
    try:
        chaos.until(writing(lambda: landed() >= 10), "the writers committed nothing before the kill", within=RECOVERY_SECONDS, every=0.2)
        for _ in range(KILLS):
            chaos.until(writing(lambda: flying[0] >= 2), "no commit was in flight to kill", within=RECOVERY_SECONDS, every=0.01)
            chaos.killed(catalog, container="catalog")
            drills.serving(table)
            if lost():
                break
        before = landed()
        chaos.until(writing(lambda: landed() >= before + 10), "the writers committed nothing after the kill", within=RECOVERY_SECONDS, every=0.2)
    finally:
        stop.set()
        for thread in writers:
            thread.join(timeout=120)

    assert failed == [], f"a writer failed the way no outage explains: {failed[0]!r}"
    assert lost(), f"none of {KILLS} kills met a commit in flight, so none lost a commit's answer"
    drills.assert_correct([table], since=started)
