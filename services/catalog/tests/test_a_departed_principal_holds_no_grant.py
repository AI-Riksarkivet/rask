"""One estate-admin door removes a departed principal from every grant and records the principal deleted ([[LH-233]]).

A person's grants are tuples naming their principal on objects of every type the model defines, written by many doors
(the members door, the per-object grant doors, the raw tuple door), so offboarding was a hunt through the store one
tuple at a time. ``DELETE /management/v1/access/principals/{principal}`` reads every tuple naming the principal, revokes
them, records the principal deleted in the catalog's database and announces ``principal_deleted`` on the control lane, so
a client holding something for the person can erase it (CTL-022); ``GET`` on the same path answers the record. It
never strands a project: a principal holding the only direct ``admin`` of any project is refused (409, code 14) and
nothing is revoked, not even an admin it holds on another project beside a second admin, and
an estate admin cannot delete themselves, since the rung the door asks would go with them. A principal whose grants git
declares is refused too, since the next upgrade or boot would grant it again: one holding a person's rung on the estate,
which only the catalog's declaration grants, and an account the catalog's settings name. A machine role on the estate is
a door's grant ([[LH-563]]), and goes with the rest.

Every decision is the real model's, on a store of this test's own. The estate admin's ``owner`` tuple on the estate is
seeded as the catalog's declaration grants it; everything else goes through the doors.
"""

import asyncio
import importlib
import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from hashlib import blake2s
from pathlib import Path
from typing import Any, LiteralString

import httpx
import psycopg
import pytest
from lance_namespace import ServiceUnavailableError
from psycopg import sql

from catalog.api.dependencies import get_control_emitter
from catalog.core.config import get_settings
from catalog.core.database import DatabaseSettings, open_database
from catalog.services import principals
from lakehouse.governed import fga_ids, fga_reads
from tests.control_emitter_double import ControlEmitterDouble
from tests.postgres_server import ROLES, PostgresDatabase


#: The Lance Namespace spec's error codes a stock client reads (`spec.yaml`).
INVALID_INPUT, CONCURRENT_MODIFICATION, SERVICE_UNAVAILABLE, INTERNAL = 13, 14, 17, 18
PRINCIPALS = "/management/v1/access/principals"
ALICE = "dex~alice"


def _ok(response: httpx.Response) -> httpx.Response:
    assert response.status_code == 200, response.text
    return response


@pytest.fixture
def estate(governed_catalog: Any) -> tuple[Any, ControlEmitterDouble]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """root owns the estate and creates ``acme``; alice holds tuples through three doors, and bob is acme's second admin."""
    catalog = governed_catalog
    recorder = ControlEmitterDouble()
    catalog.client.app.dependency_overrides[get_control_emitter] = lambda: recorder
    catalog.store.write((catalog.person("root"), "admin", "estate:rask"))
    _ok(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=catalog.bearer("root")))
    for user, relation in (("dex~alice", "admin"), ("dex~bob", "admin"), ("dex~gina", "member")):
        _ok(catalog.client.put("/management/v1/projects/acme/members", json={"user": user, "relation": relation}, headers=catalog.bearer("root")))
    for relation, obj in (("assignee", "role:stewards"), ("curator", "classification:pii"), ("event_reader", "estate:rask")):
        tuple_body = {"user": fga_ids.user_object(ALICE), "relation": relation, "object": obj}
        _ok(catalog.client.post("/management/v1/access/tuples", json=tuple_body, headers=catalog.bearer("root")))
    return catalog, recorder


def _naming(catalog: Any, principal: str) -> set[tuple[str, str, str]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    return {t for t in catalog.store.tuples() if t[0] == fga_ids.user_object(principal)}


def test_the_door_revokes_every_grant_naming_the_principal_and_records_it_deleted(estate: tuple[Any, ControlEmitterDouble]) -> None:
    catalog, recorder = estate
    before = catalog.store.tuples()
    assert _naming(catalog, ALICE) == {
        (fga_ids.user_object(ALICE), "admin", "project:acme"),
        (fga_ids.user_object(ALICE), "assignee", "role:stewards"),
        (fga_ids.user_object(ALICE), "curator", "classification:pii"),
        (fga_ids.user_object(ALICE), "event_reader", "estate:rask"),
    }

    deleted = _ok(catalog.client.delete(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))).json()

    assert _naming(catalog, ALICE) == set()
    assert {t for t in before if t[0] != fga_ids.user_object(ALICE) and t[1] != fga_reads.ADMIN_REVISION} <= catalog.store.tuples()
    record = _ok(catalog.client.get(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))).json()
    assert (record["principal"], record["kind"], record["deleted_by"], record["deleted_at"] is not None) == (ALICE, "person", "dex~root", True)
    assert deleted["revoked"] == 4
    assert [(e.action, e.object_type, e.object_id, e.extra["subject"]) for e in recorder.events if e.action == "principal_deleted"] == [
        ("principal_deleted", "principal", fga_ids.user_object(ALICE), fga_ids.user_object(ALICE))
    ]


@pytest.mark.parametrize(
    ("caller", "principal", "arrange", "answer"),
    [
        pytest.param("root", ALICE, "sole-admin-of-beta", (409, CONCURRENT_MODIFICATION), id="the-last-admin-of-one-of-its-projects"),
        pytest.param("carol", "dex~carol", "estate-admin-through-a-role", (400, INVALID_INPUT), id="the-estate-admin-themself"),
        pytest.param("root", ALICE, "an-estate-authority", (400, INVALID_INPUT), id="a-principal-the-bootstrap-hook-grants"),
        pytest.param("root", ALICE, "named-in-the-catalogs-settings", (400, INVALID_INPUT), id="an-account-the-catalog-seeds-from-its-settings"),
    ],
)
def test_a_deletion_that_would_strand_an_administration_or_undo_git_is_refused_whole(  # noqa: PLR0913 - parametrized
    estate: tuple[Any, ControlEmitterDouble], monkeypatch: pytest.MonkeyPatch, caller: str, principal: str, arrange: str, answer: tuple[int, int]
) -> None:
    catalog, recorder = estate
    if arrange == "sole-admin-of-beta":
        # alice's admin on acme is revocable (bob is acme's other admin); on beta it is the only one.
        beta = "/management/v1/projects/beta/members"
        _ok(catalog.client.post("/management/v1/projects", json={"id": "beta"}, headers=catalog.bearer("root")))
        _ok(catalog.client.put(beta, json={"user": ALICE, "relation": "admin"}, headers=catalog.bearer("root")))
        _ok(catalog.client.request("DELETE", beta, json={"user": "dex~root", "relation": "admin"}, headers=catalog.bearer("root")))
    if arrange == "estate-admin-through-a-role":
        # carol holds the estate rung only through a role, so no tuple of hers is on the estate itself.
        catalog.store.write(("role:ops#assignee", "owner", "estate:rask"), (catalog.person("carol"), "assignee", "role:ops"))
    if arrange == "an-estate-authority":
        # As the catalog's declaration grants the estate admin its values name.
        catalog.store.write((catalog.person("alice"), "admin", "estate:rask"))
    if arrange == "named-in-the-catalogs-settings":
        # As the chart names a provenance writer, which the catalog grants at boot.
        monkeypatch.setenv("LANCE_PROVENANCE_WRITERS", json.dumps([ALICE]))
        get_settings.cache_clear()
    before = catalog.store.tuples()

    refused = catalog.client.delete(f"{PRINCIPALS}/{principal}", headers=catalog.bearer(caller))

    assert (refused.status_code, refused.json()["code"]) == answer, refused.text
    assert catalog.store.tuples() == before
    assert catalog.client.get(f"{PRINCIPALS}/{principal}", headers=catalog.bearer("root")).status_code == 404
    assert [e.action for e in recorder.events if e.action == "principal_deleted"] == []


def test_a_principal_record_a_restore_brings_back_changes_no_door(estate: tuple[Any, ControlEmitterDouble]) -> None:
    """An object a control-root restore brings back under ``_principals/`` is read by no door: the catalog's record of
    a principal lives in its database, so the read door and the offboarding door answer as though it were absent."""
    catalog, _ = estate
    restored = Path(get_settings().registry_root) / "_principals" / f"{blake2s(ALICE.encode('utf-8')).hexdigest()}.json"
    restored.parent.mkdir(parents=True)
    restored.write_text('{"principal": "dex~alice", "kind": "robot"', encoding="utf-8")
    unrecorded = catalog.client.get(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root")).status_code

    deleted = catalog.client.delete(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))

    record = catalog.client.get(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))
    assert (unrecorded, deleted.status_code, deleted.json()["revoked"], record.json()) == (404, 200, 4, deleted.json()["record"]), deleted.text


@pytest.fixture
def two_connections(catalog_database: PostgresDatabase, monkeypatch: pytest.MonkeyPatch) -> PostgresDatabase:
    """The catalog's pool at two connections, for two requests held at once."""
    monkeypatch.setenv("RASK_DATABASE_POOL_SIZE", "2")
    return catalog_database


def _waiting_on_a_lock(database: PostgresDatabase, count: int) -> None:
    """Return once ``count`` of the catalog's sessions wait on a lock, or fail after 20 s."""
    deadline = time.monotonic() + 20
    with psycopg.connect(database.url, autocommit=True) as watcher:
        while time.monotonic() < deadline:
            row = watcher.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = 'rask-catalog' AND wait_event_type = 'Lock'"
            ).fetchone()
            if row is not None and row[0] == count:
                return
            time.sleep(0.05)
    pytest.fail(f"{count} offboardings never waited on the held lock together")


def test_two_offboardings_at_once_record_one_deletion(two_connections: PostgresDatabase, estate: tuple[Any, ControlEmitterDouble]) -> None:
    catalog, _ = estate
    headers = catalog.bearer("root")
    with psycopg.connect(two_connections.url) as holder, ThreadPoolExecutor(max_workers=2) as pool:
        # Held on a connection of the test's own, so both inserts reach the table and wait on it together.
        holder.execute("LOCK TABLE rask.principals IN SHARE MODE")
        started = threading.Barrier(2)

        def offboard() -> httpx.Response:
            started.wait()
            return catalog.client.delete(f"{PRINCIPALS}/{ALICE}", headers=headers)

        answers = [pool.submit(offboard) for _ in range(2)]
        _waiting_on_a_lock(two_connections, 2)
        holder.commit()
        first, second = (answer.result(timeout=30) for answer in answers)

    record = catalog.client.get(f"{PRINCIPALS}/{ALICE}", headers=headers)
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert first.json()["record"] == second.json()["record"] == record.json()


#: The advisory lock the test holds and a commit's deferred check waits on.
_COMMIT_LOCK = 862
#: A deferred constraint on the principals table, checked when an insert's transaction commits.
_AT_THE_COMMIT = (
    "CREATE CONSTRAINT TRIGGER at_the_commit AFTER INSERT ON rask.principals DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION rask.at_the_commit()"
)
#: The check waits on the test's lock, so the commit is held where its outcome is decided.
_WAITS = sql.SQL(
    "CREATE FUNCTION rask.at_the_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_advisory_xact_lock({}); RETURN NULL; END $$"
).format(sql.Literal(_COMMIT_LOCK))
#: The check refuses the commit, as a constraint the write breaks does.
_REFUSES = (
    "CREATE FUNCTION rask.at_the_commit() RETURNS trigger LANGUAGE plpgsql AS "
    "$$ BEGIN RAISE EXCEPTION 'refused at the commit' USING ERRCODE = 'check_violation'; END $$"
)


def _checked_at_the_commit(database: PostgresDatabase, check: sql.Composed | LiteralString) -> None:
    """Give this test's principals table ``check`` as a deferred constraint, as the server's superuser."""
    with psycopg.connect(database.url, autocommit=True) as owner:
        owner.execute(check)
        owner.execute(_AT_THE_COMMIT)


@pytest.mark.parametrize(
    ("held", "answer"),
    [
        pytest.param(("LOCK TABLE rask.principals IN SHARE MODE", ()), (503, SERVICE_UNAVAILABLE, "ServiceUnavailableError"), id="at-its-insert"),
        pytest.param(("SELECT pg_advisory_lock(%s)", (_COMMIT_LOCK,)), (500, INTERNAL, "CommitOutcomeUnknownError"), id="at-its-commit"),
    ],
)
def test_an_offboarding_the_database_stops_answering_is_retryable_only_before_its_commit(
    estate: tuple[Any, ControlEmitterDouble], catalog_database: PostgresDatabase, held: tuple[LiteralString, tuple[int, ...]], answer: tuple[int, int, str]
) -> None:
    # The catalog's session waits on what the test holds, at its insert or at the commit's deferred check, and is ended
    # there as an operator or a failover ends one. Ended before its commit, the write applied nothing, so a retry is what
    # heals it; ended at its commit, the write may have applied, which a retry could apply twice (ADR 0230 §6).
    catalog, _ = estate
    _checked_at_the_commit(catalog_database, _WAITS)
    statement, params = held
    with psycopg.connect(catalog_database.url) as holder, ThreadPoolExecutor(max_workers=1) as pool:
        holder.execute(statement, params)
        offboarding = pool.submit(catalog.client.delete, f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))
        _waiting_on_a_lock(catalog_database, 1)
        holder.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND application_name = 'rask-catalog'")
        holder.commit()
        answered = offboarding.result(timeout=30)

    body = answered.json()
    assert (answered.headers["content-type"], (answered.status_code, body["code"], body["title"])) == ("application/problem+json", answer)


def test_an_offboarding_whose_commit_the_database_refuses_applies_nothing(estate: tuple[Any, ControlEmitterDouble], catalog_database: PostgresDatabase) -> None:
    # The server's answer refusing the commit proves the transaction rolled back: the door answers the refusal, after
    # which no record stands, never the error of a commit that may have applied.
    catalog, _ = estate
    _checked_at_the_commit(catalog_database, _REFUSES)

    refused = catalog.client.delete(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))

    record = catalog.client.get(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))
    assert ((refused.status_code, refused.json()["code"], refused.json()["title"]), record.status_code) == ((500, INTERNAL, "InternalError"), 404)


#: Far above the milliseconds a door takes to reach the lock the test holds, so the deadline passes while it waits there.
_DEADLINE_SECONDS = 3.0


@pytest.fixture
def short_deadline(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The catalog app built under a request deadline of ``_DEADLINE_SECONDS``. The deadline is read when the app is built,
    at import, so the module is reloaded with it, and reloaded again without it once the test is done. A test names it in
    ``usefixtures``, which pytest sets up before the test's own fixtures, so ``estate`` serves the app this builds."""
    import catalog.main

    monkeypatch.setenv("LANCE_REQUEST_DEADLINE_SECONDS", str(_DEADLINE_SECONDS))
    get_settings.cache_clear()
    importlib.reload(catalog.main)
    yield
    monkeypatch.delenv("LANCE_REQUEST_DEADLINE_SECONDS")
    get_settings.cache_clear()
    importlib.reload(catalog.main)
    get_settings.cache_clear()


def _until_the_catalog_rests(database: PostgresDatabase) -> None:
    """Return once none of the catalog's sessions is running a statement or holding a transaction, or fail after 20 s."""
    deadline = time.monotonic() + 20
    with psycopg.connect(database.url, autocommit=True) as watcher:
        while time.monotonic() < deadline:
            row = watcher.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = 'rask-catalog' AND state <> 'idle'"
            ).fetchone()
            if row is not None and row[0] == 0:
                return
            time.sleep(0.05)
    pytest.fail("the catalog's door never ended its work with the database")


@pytest.mark.usefixtures("short_deadline")
@pytest.mark.parametrize(
    ("held", "answer", "recorded"),
    [
        pytest.param(("SELECT pg_advisory_lock(%s)", (_COMMIT_LOCK,)), (500, INTERNAL, "CommitOutcomeUnknownError"), 200, id="held-at-its-commit"),
        pytest.param(("LOCK TABLE rask.principals IN SHARE MODE", ()), (503, SERVICE_UNAVAILABLE, "Service Unavailable"), 404, id="held-before-its-commit"),
    ],
)
def test_an_offboarding_past_its_request_deadline_is_answered_by_whether_its_commit_began(
    estate: tuple[Any, ControlEmitterDouble],
    catalog_database: PostgresDatabase,
    held: tuple[LiteralString, tuple[int, ...]],
    answer: tuple[int, int, str],
    recorded: int,
) -> None:
    # Held at its commit's deferred check past the deadline, the offboarding may have applied, and does once let through:
    # its caller is never asked to retry. Held before its commit, its caller is answered 503, and the commit it reaches
    # afterwards is refused, so its record never lands (ADR 0203, amendment of 2026-11-28).
    catalog, _ = estate
    _checked_at_the_commit(catalog_database, _WAITS)
    statement, params = held
    with psycopg.connect(catalog_database.url) as holder:
        holder.execute(statement, params)
        answered = catalog.client.delete(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))
    _until_the_catalog_rests(catalog_database)

    record = catalog.client.get(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))
    body = answered.json()
    assert ((answered.status_code, body["code"], body["title"]), record.status_code) == (answer, recorded)


@pytest.mark.usefixtures("short_deadline")
def test_a_read_past_its_request_deadline_is_answered_503_whatever_was_committed_before_it(
    estate: tuple[Any, ControlEmitterDouble], catalog_database: PostgresDatabase
) -> None:
    # A read applies nothing, and a commit is marked on its own request alone: a read held past its deadline after an
    # offboarding committed is the outage a retry may heal.
    catalog, _ = estate
    _ok(catalog.client.delete(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root")))
    with psycopg.connect(catalog_database.url) as holder:
        holder.execute("LOCK TABLE rask.principals IN ACCESS EXCLUSIVE MODE")
        answered = catalog.client.get(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))
    _until_the_catalog_rests(catalog_database)

    body = answered.json()
    assert (answered.status_code, body["code"], body["title"]) == (503, SERVICE_UNAVAILABLE, "Service Unavailable")


async def _until_its_session_ends(database: PostgresDatabase) -> None:
    """Return once the catalog has no session on ``database``, or fail after 20 s."""
    deadline = time.monotonic() + 20
    with psycopg.connect(database.url, autocommit=True) as watcher:
        while time.monotonic() < deadline:
            row = watcher.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = 'rask-catalog'").fetchone()
            if row is not None and row[0] == 0:
                return
            await asyncio.sleep(0.05)
    pytest.fail("the server never ended the catalog's idle session")


@pytest.mark.asyncio
async def test_a_commit_whose_session_the_database_ended_while_it_idled_is_the_outage(catalog_database: PostgresDatabase) -> None:
    # The server ends a session idle in its transaction before the commit reaches it (FATAL 25P03), so nothing applied: the
    # commit answers the outage a retry may heal, never an outcome it cannot tell. A second of idleness, far above the gap
    # between the write's own statements, so the session ends between the block and its commit.
    with psycopg.connect(catalog_database.url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("ALTER ROLE {} IN DATABASE {} SET idle_in_transaction_session_timeout = '1s'").format(
                sql.Identifier(ROLES.catalog), sql.Identifier(catalog_database.name)
            )
        )
    database = open_database(DatabaseSettings())
    try:
        with pytest.raises(ServiceUnavailableError, match="did not answer"):
            async with database.writing() as session:
                await principals.record_deletion(session, ALICE, deleted_by="dex~root")
                await _until_its_session_ends(catalog_database)
    finally:
        await database.dispose()
