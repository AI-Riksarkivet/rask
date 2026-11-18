"""What a dead creation left reaches no live tenant, and nothing it left blocks git's declaration for ever (ADR 0241 §9,
§10, ADR 0245).

A project's id may be held again by another tenant once it is deleted, so every record naming a project names the
creation it was made under (``project_uuid``): a warehouse's record, its bucket's claim, and the project's maintenance
policy, and a record naming none does not parse. A warehouse's create writes its record before it resolves its project
again, and the project's delete marks the project before it reads its warehouses, so no warehouse goes live under a
creation that ended. A bucket is a name every creation shares, so its claim stays the arbiter of who may write in it.
Driven on the deployed shape: the real app with authorization on (a loopback Dex, a real OpenFGA store of the test's
own) over an S3 store.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from functools import partial
from typing import Any, cast

import boto3
import lance
import pytest
from fastapi import FastAPI

from catalog.services import projects, tenancy, tenancy_converge, warehouses
from catalog.services.control_records import write_json
from lakehouse import control_prefixes
from lakehouse import maintenance_policies as policies
from lakehouse.governed import fga
from lakehouse.governed.audit import AUDIT_LOGGER, AuditAction
from lakehouse.governed.tenancy import PROJECT_UUID, UUID, warehouses_of
from lakehouse.records import create_json


IDP, ROOT = "dex", "estate:rask"
PROJECT = "acme"


def _admins(estate: Any, *people: str) -> list[dict[str, str]]:  # noqa: ANN401 — the conftest's Estate
    """Each of ``people`` made an estate admin, and the headers each signs in with."""
    for person in people:
        estate.store.write((fga.user_object(f"{IDP}~{person}"), "admin", ROOT))
    return [estate.bearer(person) for person in people]


def _settings(estate: Any) -> Any:  # noqa: ANN401 — the conftest's Estate and the catalog's settings
    return estate.client.app.state.settings


def _stored_warehouse(estate: Any, record: dict[str, str]) -> None:  # noqa: ANN401 — the conftest's Estate
    """``record`` stored as the warehouse registry keeps it, past every door: a state no create of this catalog writes."""
    settings = _settings(estate)
    write_json(settings.registry_root, settings.storage_options(), f"{control_prefixes.WAREHOUSES}/{record['id']}.json", record)


def _project_uuid(estate: Any) -> str:  # noqa: ANN401 — the conftest's Estate
    settings = _settings(estate)
    record = projects.get_project(settings.registry_root, settings.storage_options(), PROJECT)
    assert record is not None
    return str(record[UUID])


def _held(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> tuple[threading.Event, threading.Event]:  # noqa: ANN401 — the module whose function is held
    """Hold the first call of ``owner.name`` until released: one door stopped at one step while the others run whole."""
    arrived, release = threading.Event(), threading.Event()
    real = getattr(owner, name)

    def _call(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the held function's own arguments and answer
        if not arrived.is_set():
            arrived.set()
            assert release.wait(30), f"the held {name} was never released"
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, _call)
    return arrived, release


def _converge(estate: Any, **kwargs: Any) -> tenancy_converge.TenancyConverged:  # noqa: ANN401 — the conftest's Estate, converge's own arguments
    settings, app = _settings(estate), cast(FastAPI, estate.client.app)
    assert estate.client.portal is not None
    return estate.client.portal.call(partial(tenancy_converge.converge, settings, app.state.fga, connections=app.state.connections, **kwargs))


def _s3(estate: Any) -> Any:  # noqa: ANN401 — the conftest's Estate and a boto3 client
    settings = _settings(estate)
    return boto3.client("s3", endpoint_url=settings.s3_endpoint, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")


def _keys(estate: Any, bucket: str) -> list[str]:  # noqa: ANN401 — the conftest's Estate
    return sorted(o["Key"] for page in _s3(estate).get_paginator("list_objects_v2").paginate(Bucket=bucket) for o in page.get("Contents", []))


def _catalog_rows(estate: Any, bucket: str) -> list[str]:  # noqa: ANN401 — the conftest's Estate
    """The ids the directory catalog of ``bucket`` (its ``__manifest`` table) holds a row for."""
    so = _settings(estate).storage_options()
    table = lance.dataset(f"s3://{bucket}/__manifest", storage_options=so.in_clear() or None).to_table(columns=["object_id"])
    return sorted(str(row) for row in table.column("object_id").to_pylist())


def _purge(estate: Any, warehouse_id: str, headers: dict[str, str], **params: str) -> Any:  # noqa: ANN401 — the conftest's Estate and an httpx response
    return estate.client.delete(f"/management/v1/warehouses/{warehouse_id}", params={"purge_bucket": "true", **params}, headers=headers)


def _rows(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """The audit trail's bucket purge rows, in order."""
    return [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__.get("audit.action") == AuditAction.BUCKET_PURGE.value]


def _beta(estate: Any, erin: dict[str, str]) -> None:  # noqa: ANN401 — the conftest's Estate
    assert estate.client.post("/management/v1/projects", json={"id": "beta"}, headers=erin).status_code == 200


def _a_create_held_before_its_record_across_its_projects_delete(estate: Any, monkeypatch: pytest.MonkeyPatch, alice: dict[str, str]) -> list[int]:  # noqa: ANN401
    """alice's create of ``wh-a`` on ``wh-a-bucket``, held after its claim and before its record while acme is deleted.
    Released, it writes its record naming acme's dead creation, resolves acme again, finds it gone, ends its record and
    answers 404 (`ChildUnderLiveParent`): the bucket's claim names the dead creation and no record names it."""
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    arrived, release = _held(monkeypatch, warehouses, "create_warehouse_record")
    answered: list[int] = []
    made = {"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}
    making = threading.Thread(target=lambda: answered.append(estate.client.post("/management/v1/warehouses", json=made, headers=alice).status_code))
    making.start()
    assert arrived.wait(30), "alice's create never reached its record write"
    deleted = estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice)
    release.set()
    making.join(30)
    assert (deleted.status_code, answered, estate.record("wh-a")) == (200, [404], None), deleted.text
    return answered


def test_a_warehouse_record_naming_no_creation_is_no_record_and_a_body_naming_one_is_refused(estate: Any) -> None:  # noqa: ANN401
    """A warehouse record naming no creation of its project is none a create of this catalog writes: it does not parse,
    so the project's delete, which must know every warehouse of the project, refuses (500, code 18) and deletes nothing,
    and a create naming the creation in its body is refused 422 (code 13)."""
    (alice,) = _admins(estate, "alice")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    _stored_warehouse(estate, {"id": "wh-legacy", "bucket": "wh-legacy-bucket", "root_uri": "s3://wh-legacy-bucket", "project": PROJECT})

    deleted = estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice)
    assert (deleted.status_code, deleted.json().get("code")) == (500, 18), deleted.text
    assert estate.client.get(f"/management/v1/projects/{PROJECT}/members", headers=alice).status_code == 200, "the delete took the project"

    made = estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}, headers=alice)
    named = estate.client.post(
        "/management/v1/warehouses", json={"id": "wh-b", "project": PROJECT, "bucket": "wh-b-bucket", PROJECT_UUID: "chosen"}, headers=alice
    )
    assert made.status_code == 200, made.text
    assert (estate.record("wh-a") or {}).get(PROJECT_UUID) == _project_uuid(estate)
    assert (named.status_code, named.json()["code"], estate.record("wh-b")) == (422, 13, None), named.text


def test_the_tick_ends_a_warehouse_a_dead_creation_left_creating_and_sweeps_what_was_written_on_an_ended_one(estate: Any) -> None:  # noqa: ANN401
    """A warehouse record left ``creating`` naming a creation of acme that is not live is ended, never finished under the
    live one (`TickChecksParent`, `ChildUnderLiveParent`); a tuple a door wrote on beta's creation after beta's delete
    revoked it is swept from that dead object (`TickSweeps`, `DeadObjectsSwept`)."""
    (alice,) = _admins(estate, "alice")
    for project in (PROJECT, "beta"):
        assert estate.client.post("/management/v1/projects", json={"id": project}, headers=alice).status_code == 200
    settings = _settings(estate)
    beta = f"project:{(projects.get_project(settings.registry_root, settings.storage_options(), 'beta') or {})[UUID]}"
    assert estate.client.delete("/management/v1/projects/beta", headers=alice).status_code == 200
    late = (fga.user_object(f"{IDP}~bob"), "member", beta)
    estate.store.write(late)
    dead = {"id": "wh-x", "bucket": "wh-x-bucket", "root_uri": "s3://wh-x-bucket", "project": PROJECT, UUID: "b" * 32}
    _stored_warehouse(estate, {**dead, PROJECT_UUID: "f" * 32, "state": "creating", "created_at": "2026-01-01T00:00:00+00:00"})

    converged = _converge(estate, bound=timedelta(0))

    stored = warehouses.get_warehouse_record(settings.registry_root, settings.storage_options(), "wh-x")
    assert ("warehouse:wh-x" in converged.ended, stored) == (True, None), converged
    assert (beta in converged.swept, late in estate.store.tuples()) == (True, False), converged
    assert not [t for t in estate.store.tuples() if f"warehouse:{'b' * 32}" in (t[0], t[2])], "the dead creation's warehouse was seeded"


def test_a_project_policy_governs_only_the_buckets_of_the_creation_that_set_it(estate: Any) -> None:  # noqa: ANN401
    """A warehouse record naming acme at a dead creation is no warehouse of acme's live one: acme's policy door answers
    acme has no warehouse, its listing names no such bucket, and a policy record names the creation that wrote it: one an
    earlier creation left is not acme's, and governs no bucket claimed by another (ADR 0241 §9 P2)."""
    (alice,) = _admins(estate, "alice")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    dead = "d" * 32
    _stored_warehouse(
        estate,
        {
            "id": "wh-dead",
            "bucket": "wh-dead-bucket",
            "root_uri": "s3://wh-dead-bucket",
            "project": PROJECT,
            UUID: "e" * 32,
            PROJECT_UUID: dead,
            "state": "live",
        },
    )
    policy = f"/management/v1/project/{PROJECT}/policy"

    refused = estate.client.post(f"{policy}/set", json={"cleanup_enabled": False}, headers=alice)
    listed = estate.client.get(f"/management/v1/projects/{PROJECT}/policies", headers=alice)

    assert (refused.status_code, listed.json()["buckets"], listed.json()["warehouses"]) == (400, [], []), (refused.text, listed.text)

    made = estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}, headers=alice)
    assert made.status_code == 200, made.text
    held = estate.client.post(f"{policy}/set", json={"cleanup_enabled": False}, headers=alice)
    assert (held.status_code, held.json().get("buckets")) == (200, ["wh-a-bucket"]), held.text
    settings = _settings(estate)
    so = settings.storage_options()
    stored = policies.get_policy(settings.registry_root, so, "project", PROJECT)
    assert (stored or {}).get(PROJECT_UUID) == _project_uuid(estate)

    # The same record as one an earlier creation of acme left: it governs neither acme's bucket nor is acme's to read.
    policies.put_policy(settings.registry_root, so, {**(stored or {}), PROJECT_UUID: dead})
    governing = policies.governing_policy(
        settings.registry_root, so, claims_root=settings.registry_root, location="s3://wh-a-bucket/sales/t", table_uuid="t" * 32, namespace_uuids=[]
    )
    described = estate.client.post(f"{policy}/describe", headers=alice)
    assert (governing, described.json()["set"]) == (None, False), described.text
    assert warehouses_of(warehouses.list_warehouses(settings.registry_root, so), _project_uuid(estate)) == [estate.record("wh-a")]

    # A set naming one field takes every other from no record an earlier creation left: acme never set a retention.
    policies.put_policy(settings.registry_root, so, {**(stored or {}), PROJECT_UUID: dead, "retain_versions": 7, "retention_days": 30})
    partial_set = estate.client.post(f"{policy}/set", json={"compact_enabled": True}, headers=alice)
    written = policies.get_policy(settings.registry_root, so, "project", PROJECT) or {}
    assert partial_set.status_code == 200, partial_set.text
    assert [written.get(field) for field in ("retain_versions", "retention_days", "cleanup_enabled", PROJECT_UUID)] == [
        None,
        None,
        True,
        _project_uuid(estate),
    ], written


def test_a_bucket_git_declares_is_claimed_once_the_dead_creation_that_claimed_it_is_released(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """alice's create of ``wh-a`` ran across acme's delete and left a claim on ``wh-a-bucket`` naming the dead creation,
    which no record names. Git then declares acme for erin and ``wh-a`` on that bucket: a tick that kept the claim would
    fail ``wh-a`` for ever (`GrantsClaimKept`). The tick reports the claim by its bucket and, since the bucket holds only
    what a create writes, releases it, and the next tick makes git's ``wh-a`` in erin's acme (`ClaimFreed`)."""
    alice, _erin = _admins(estate, "alice", "erin")
    _a_create_held_before_its_record_across_its_projects_delete(estate, monkeypatch, alice)

    estate.declare(projects=[{"id": PROJECT, "admins": [f"{IDP}~erin"]}], warehouses=[{"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}])
    refused = estate.tick()
    assert [f for f in refused.failed if f.startswith("warehouse:wh-a")], refused
    released = _converge(estate, bound=timedelta(0))
    converged = estate.tick()

    made = estate.record("wh-a") or {}
    assert (released.orphans, released.released) == (["bucket:wh-a-bucket"], ["wh-a-bucket"]), released
    assert (converged.failed, made.get(PROJECT_UUID), made.get("managed_by")) == ([], _project_uuid(estate), "git"), converged
    assert (f"project:{_project_uuid(estate)}", "project", f"warehouse:{made[UUID]}") in estate.store.tuples()


def test_a_warehouse_made_across_its_projects_delete_keeps_the_project_and_its_bucket(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """alice's create of ``wh-a`` resolved acme again after its record and is held before its seed while acme's delete
    runs: the delete marks acme, reads its warehouses, finds ``wh-a`` still creating and refuses 409 (`CountsCreating`,
    `MarksFirst`); released, ``wh-a`` goes live under acme's creation, and no tick ends it or releases its claim
    (`BornLeftKept`). Its project is deleted once it is."""
    (alice,) = _admins(estate, "alice")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    arrived, release = _held(monkeypatch, warehouses, "ensure_catalog_row")
    answered: list[int] = []
    made = {"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}
    making = threading.Thread(target=lambda: answered.append(estate.client.post("/management/v1/warehouses", json=made, headers=alice).status_code))
    making.start()
    assert arrived.wait(30), "alice's create never reached its catalog row"
    refused = estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice)
    release.set()
    making.join(30)
    kept = _converge(estate, bound=timedelta(0))
    settings = _settings(estate)

    assert (refused.status_code, answered, (estate.record("wh-a") or {}).get("state")) == (409, [200], "live"), refused.text
    assert (kept.ended, kept.released) == ([], []), kept
    assert warehouses.bucket_claim(settings.registry_root, settings.storage_options(), bucket="wh-a-bucket") is not None
    assert estate.client.delete("/management/v1/warehouses/wh-a", headers=alice).status_code == 200
    assert estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice).status_code == 200


def test_a_purge_holds_the_claim_it_deletes_under_and_no_create_lands_on_the_bucket_meanwhile(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """The estate owner's purge of a dead claim on a bucket holding bytes no create wrote takes the claim for itself
    before its first delete (ADR 0241 §10 Q1): held there, beta's create on the bucket is refused 409 (code 14), so no
    live tenant's bytes are among what it deletes (`PurgeTakesNothingLive`); once it ended, beta takes the bucket."""
    alice, erin = _admins(estate, "alice", "erin")
    _a_create_held_before_its_record_across_its_projects_delete(estate, monkeypatch, alice)
    _s3(estate).put_object(Bucket="wh-a-bucket", Key="elsewhere/bytes", Body=b"not the create's")
    _beta(estate, erin)
    assert _converge(estate, bound=timedelta(0)).released == []
    arrived, release = _held(monkeypatch, warehouses, "purge_bucket")
    answered: list[Any] = []
    purging = threading.Thread(target=lambda: answered.append(_purge(estate, "wh-a-bucket", erin)))
    purging.start()
    assert arrived.wait(30), "the purge never reached its delete"
    taken = estate.client.post("/management/v1/warehouses", json={"id": "wh-e", "project": "beta", "bucket": "wh-a-bucket"}, headers=erin)
    release.set()
    purging.join(30)
    retaken = estate.client.post("/management/v1/warehouses", json={"id": "wh-e", "project": "beta", "bucket": "wh-a-bucket"}, headers=erin)

    (purged,) = answered
    assert (taken.status_code, taken.json()["code"]) == (409, 14), taken.text
    assert (purged.status_code, purged.json()["bucket_purged"]) == (200, True), purged.text
    assert retaken.status_code == 200, retaken.text
    assert _catalog_rows(estate, "wh-a-bucket") == ["wh-e"]


def test_a_dead_claim_no_record_names_is_reported_and_the_estate_owner_purges_it_by_its_bucket(estate: Any) -> None:  # noqa: ANN401
    """A crash can leave a claim of a dead creation that no record names, on a bucket holding bytes no create writes
    (ADR 0241 §10 Q2). The tick reports it among the orphans, and the estate owner's purge door reaches it by its bucket:
    the same route, the bucket in the warehouse's place, the same relation; anyone else is answered 404."""
    alice, erin = _admins(estate, "alice", "erin")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    settings = _settings(estate)
    so = settings.storage_options()
    claim = {"bucket": "lost-bucket", "project": PROJECT, PROJECT_UUID: _project_uuid(estate), "warehouse_id": "wh-lost"}
    create_json(
        settings.registry_root, so, f"{control_prefixes.WAREHOUSES}/bucket-claims/lost-bucket.json", {**claim, "claimed_at": "2026-01-01T00:00:00+00:00"}
    )
    _s3(estate).create_bucket(Bucket="lost-bucket")
    _s3(estate).put_object(Bucket="lost-bucket", Key="elsewhere/bytes", Body=b"not the create's")
    assert estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice).status_code == 200

    reported = _converge(estate, bound=timedelta(0))
    refused = _purge(estate, "lost-bucket", estate.bearer("bob"))
    purged = _purge(estate, "lost-bucket", erin)

    assert ("bucket:lost-bucket" in reported.orphans, reported.released) == (True, []), reported
    assert refused.status_code == 404, refused.text
    assert (purged.status_code, purged.json()["bucket_purged"], purged.json()["objects_purged"]) == (200, True, 1), purged.text
    assert warehouses.bucket_claim(settings.registry_root, so, bucket="lost-bucket") is None


def test_one_unreadable_claim_stops_no_tick_and_no_claim_is_released_while_it_stands(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """One claim no catalog wrote beside alice's dead claim (ADR 0241 §10 Q3). Raised from the convergence, it would
    stop every tick and the boot backfill, so no declaration would converge. The tick names it among its failures,
    releases no claim while it stands, and converges everything else: git's project ``gamma`` is made."""
    alice, _erin = _admins(estate, "alice", "erin")
    _a_create_held_before_its_record_across_its_projects_delete(estate, monkeypatch, alice)
    settings = _settings(estate)
    write_json(settings.registry_root, settings.storage_options(), f"{control_prefixes.WAREHOUSES}/bucket-claims/junk.json", {"bucket": "junk", "project": "x"})

    converged = _converge(estate, bound=timedelta(0))
    estate.declare(projects=[{"id": "gamma", "admins": [f"{IDP}~erin"]}])
    ticked = estate.tick()

    assert ([f for f in converged.failed if "bucket-claims/junk.json" in f] != [], converged.released) == (True, []), converged
    assert converged.orphans == ["bucket:wh-a-bucket"], converged
    assert [f for f in ticked.failed if "junk.json" in f] != [] and "project:gamma" in ticked.converged, ticked
    assert warehouses.bucket_claim(settings.registry_root, settings.storage_options(), bucket="wh-a-bucket") is not None


def test_a_create_that_outlived_its_claim_writes_nothing_more_into_the_bucket(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """alice's create claimed ``wh-a-bucket`` and is held before its record while acme is deleted, the tick releases the
    dead claim and beta makes ``wh-e`` there (ADR 0241 §10 Q6b). Released, it finds its claim no longer its own and
    writes nothing more: no record, and ``wh-e``'s catalog holds no row of ``wh-a``."""
    alice, erin = _admins(estate, "alice", "erin")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    _beta(estate, erin)
    arrived, release = _held(monkeypatch, warehouses, "provision_bucket")
    answered: list[int] = []
    made = {"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}
    making = threading.Thread(target=lambda: answered.append(estate.client.post("/management/v1/warehouses", json=made, headers=alice).status_code))
    making.start()
    assert arrived.wait(30), "alice's create never reached its provision"
    assert estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice).status_code == 200
    released = _converge(estate, bound=timedelta(0))
    taken = estate.client.post("/management/v1/warehouses", json={"id": "wh-e", "project": "beta", "bucket": "wh-a-bucket"}, headers=erin)
    release.set()
    making.join(30)

    assert (released.released, taken.status_code, answered, estate.record("wh-a")) == (["wh-a-bucket"], 200, [409], None), (released, taken.text)
    assert _catalog_rows(estate, "wh-a-bucket") == ["wh-e"]


def test_a_purge_is_audited_before_its_first_delete_and_one_that_fails_after_it_says_how_far_it_got(
    estate: Any,  # noqa: ANN401 — the conftest's Estate
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A live warehouse's purge, its record's end failing once its bytes are gone (ADR 0241 §10 Q5): the trail holds the
    intent before the first delete and a failure naming how far it got, a create on the bucket is refused while the purge
    holds it, and the purge asked again resumes under the claim it took and ends audited."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    (alice,) = _admins(estate, "alice")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    assert estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}, headers=alice).status_code == 200
    real_end = warehouses.end_warehouse

    def _refused(*_args: Any, **_kwargs: Any) -> None:  # noqa: ANN401 — end_warehouse's own arguments
        raise OSError("the store refused the delete")

    monkeypatch.setattr(warehouses, "end_warehouse", _refused)
    failed = _purge(estate, "wh-a", alice)
    monkeypatch.setattr(warehouses, "end_warehouse", real_end)
    taken = estate.client.post("/management/v1/warehouses", json={"id": "wh-b", "project": PROJECT, "bucket": "wh-a-bucket"}, headers=alice)
    resumed = _purge(estate, "wh-a", alice)

    assert (failed.status_code, taken.status_code, resumed.status_code) == (503, 409, 200), (failed.text, taken.text, resumed.text)
    assert [(r["audit.outcome"], r["audit.stage"], r["audit.dead_tenant"]) for r in _rows(caplog)] == [
        ("allow", "claim_taken", False),
        ("failure", "bytes_deleted", False),
        ("allow", "claim_taken", False),
        ("success", "claim_released", False),
    ], _rows(caplog)
    settings = _settings(estate)
    assert tenancy.resolve(settings.registry_root, settings.storage_options(), "warehouse", "wh-a") is None
