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

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import blake2s
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from catalog.api.dependencies import get_control_emitter
from catalog.core.config import get_settings
from lakehouse.governed import fga_ids, fga_reads
from tests.control_emitter_double import ControlEmitterDouble
from tests.postgres_server import PostgresDatabase


#: The Lance Namespace spec's error codes a stock client reads (`spec.yaml`).
INVALID_INPUT, CONCURRENT_MODIFICATION = 13, 14
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
