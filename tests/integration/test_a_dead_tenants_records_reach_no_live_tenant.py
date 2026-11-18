"""What a dead tenant left reaches no live tenant, and nothing it left blocks git's declaration for ever (ADR 0241 §9).

A project's id may be held again by another tenant once it is deleted, so every record naming a project names the
incarnation it was made under (``project_incarnation``): a warehouse's record, its bucket's claim, and the project's
maintenance policy. One function answers which of them a project's standing incarnation holds
(``incarnations.warehouses_of``), and a record naming a project and no incarnation of it is an internal error, never
another tenant's. Driven on the deployed shape: the real app with authorization on (a loopback Dex, a real OpenFGA
store of the test's own) over an S3 store.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from fastapi import FastAPI

from catalog import main as boot
from catalog.api.dependencies import get_control_emitter
from catalog.services import incarnations, projects, warehouses
from catalog.services.control_records import INCARNATION, PROJECT_INCARNATION, write_json
from lakehouse import control_prefixes
from lakehouse import maintenance_policies as policies
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed import fga
from lakehouse.governed.audit import AUDIT_LOGGER, AuditAction
from lakehouse.records import mutate_json


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


def _project_incarnation(estate: Any) -> str:  # noqa: ANN401 — the conftest's Estate
    settings = _settings(estate)
    record = projects.get_project(settings.registry_root, settings.storage_options(), PROJECT)
    assert record is not None
    return str(record[INCARNATION])


def test_a_warehouse_record_naming_its_project_and_no_incarnation_of_it_is_refused_where_it_is_read_and_never_hidden(
    estate: Any,  # noqa: ANN401 — the conftest's Estate
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A record naming acme and no incarnation of it is no create's (P1). Read as another tenant's, it would be hidden
    from acme's listing and let acme's delete pass its emptiness guard and leave it standing; so every reader of acme's
    warehouses answers it 500 (code 18), the log naming it, and the delete deletes nothing. No request reaches that
    answer: a create's record names the incarnation its fence read, and a body naming one is refused."""
    (alice,) = _admins(estate, "alice")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    _stored_warehouse(estate, {"id": "wh-legacy", "bucket": "wh-legacy-bucket", "root_uri": "s3://wh-legacy-bucket", "project": PROJECT, INCARNATION: "1" * 32})

    listed = estate.client.get(f"/management/v1/projects/{PROJECT}", headers=alice)
    deleted = estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice)

    named_in_log = [r for r in caplog.records if r.getMessage() == "record_without_project_incarnation" and getattr(r, "object", None) == "warehouse:wh-legacy"]
    assert ([(r.status_code, r.json().get("code")) for r in (listed, deleted)], len(named_in_log)) == ([(500, 18)] * 2, 2), (listed.text, deleted.text)
    assert estate.record("wh-legacy") is not None, "the record was read as another tenant's"
    assert estate.client.get(f"/management/v1/projects/{PROJECT}/members", headers=alice).status_code == 200, "the delete took the project"

    made = estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}, headers=alice)
    named = estate.client.post(
        "/management/v1/warehouses", json={"id": "wh-b", "project": PROJECT, "bucket": "wh-b-bucket", PROJECT_INCARNATION: "chosen"}, headers=alice
    )
    assert made.status_code == 200, made.text
    assert (estate.record("wh-a") or {}).get(PROJECT_INCARNATION) == _project_incarnation(estate)
    assert (named.status_code, named.json()["code"], estate.record("wh-b")) == (422, 13, None), named.text


def test_a_project_policy_governs_only_the_buckets_of_the_incarnation_that_set_it(estate: Any) -> None:  # noqa: ANN401
    """A warehouse record a dead tenant's create left names acme at another incarnation (P2). Read as acme's, it alone
    would satisfy "acme has an active warehouse" and its bucket would be governed by acme's policy; so acme's policy door
    answers acme has no warehouse, its listing names no such bucket, and a policy record names the incarnation that wrote
    it: one an earlier incarnation left is not acme's, and governs no bucket claimed by another."""
    (alice,) = _admins(estate, "alice")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    dead = "d" * 32
    _stored_warehouse(
        estate,
        {"id": "wh-dead", "bucket": "wh-dead-bucket", "root_uri": "s3://wh-dead-bucket", "project": PROJECT, INCARNATION: "e" * 32, PROJECT_INCARNATION: dead},
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
    assert (stored or {}).get(PROJECT_INCARNATION) == _project_incarnation(estate)

    # The same record as one an earlier incarnation of acme left: it governs neither acme's bucket nor is acme's to read.
    policies.put_policy(settings.registry_root, so, {**(stored or {}), PROJECT_INCARNATION: dead})
    governing = policies.governing_policy(
        settings.registry_root, so, claims_root=settings.registry_root, location="s3://wh-a-bucket/sales/t", table_uuid="t" * 32, namespace_uuids=[]
    )
    described = estate.client.post(f"{policy}/describe", headers=alice)
    assert (governing, described.json()["set"]) == (None, False), described.text
    assert warehouses.bucket_claim(settings.registry_root, so, bucket="wh-a-bucket") is not None
    assert incarnations.warehouses_of(warehouses.list_warehouses(settings.registry_root, so), PROJECT, _project_incarnation(estate)) == [estate.record("wh-a")]


def _held_record_write(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Hold the next warehouse record's write until released: the window between a create's last fence check and its
    record (ADR 0241 §8 B3)."""
    arrived, release = threading.Event(), threading.Event()
    real_record = warehouses.create_warehouse_record

    def _record(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — create_warehouse_record's own arguments
        if not arrived.is_set():
            arrived.set()
            assert release.wait(30), "the held record write was never released"
        real_record(*args, **kwargs)

    monkeypatch.setattr(warehouses, "create_warehouse_record", _record)
    return arrived, release


def _orphan_by_a_create_held_across_its_projects_delete(estate: Any, monkeypatch: pytest.MonkeyPatch, alice: dict[str, str]) -> None:  # noqa: ANN401
    """alice's create of ``wh-a`` on ``wh-a-bucket`` held between its last fence check and its record while acme is
    deleted; released, its record lands naming the dead incarnation and its birth is refused (409). A convergence that
    listed acme standing before the delete runs across it and is released last, so it finds the record naming the
    incarnation it read standing (`GrantsChildBirthUnfenced`)."""
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    arrived, release = _held_record_write(monkeypatch)
    listed, resumed = threading.Event(), threading.Event()
    real_read = projects.read_projects

    def _read_projects(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — read_projects's own arguments and answer
        answer = real_read(*args, **kwargs)
        if not listed.is_set():
            listed.set()
            assert resumed.wait(30), "the held convergence was never released"
        return answer

    monkeypatch.setattr(incarnations, "projects", SimpleNamespace(**{**vars(projects), "read_projects": _read_projects}))
    answered: list[int] = []
    made = {"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}
    making = threading.Thread(target=lambda: answered.append(estate.client.post("/management/v1/warehouses", json=made, headers=alice).status_code))
    making.start()
    assert arrived.wait(30), "alice's create never reached its record write"
    converging = threading.Thread(target=lambda: _converge(estate, bound=timedelta(minutes=10)))
    converging.start()
    assert listed.wait(30), "the convergence never listed the projects"
    deleted = estate.client.delete(f"/management/v1/projects/{PROJECT}", headers=alice)
    release.set()
    making.join(30)
    resumed.set()
    converging.join(30)
    monkeypatch.setattr(incarnations, "projects", projects)
    assert (deleted.status_code, answered) == (200, [409]), deleted.text
    assert estate.record("wh-a") is not None, "the held create wrote no record: the window was not held open"


def _converge(estate: Any, **kwargs: Any) -> incarnations.IncarnationsConverged:  # noqa: ANN401 — the conftest's Estate, converge's own arguments
    settings, app = _settings(estate), cast(FastAPI, estate.client.app)
    assert estate.client.portal is not None
    return estate.client.portal.call(partial(incarnations.converge, settings.registry_root, settings.storage_options(), app.state.fga, **kwargs))


def _keys(estate: Any, bucket: str) -> list[str]:  # noqa: ANN401 — the conftest's Estate
    settings = _settings(estate)
    s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    return sorted(o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket) for o in page.get("Contents", []))


def test_a_bucket_git_declares_is_claimed_once_the_dead_tenant_whose_create_claimed_it_is_ended_unborn(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    """alice's create of ``wh-a`` raced acme's delete and left an unborn record and a claim on ``wh-a-bucket`` naming the
    dead incarnation (P3). Git then declares acme for erin and ``wh-a`` on that bucket: a tick that ends the record and
    keeps the claim fails every tick for ever, and no door releases it (`GrantsClaimKept`). So the tick stamps the record
    ended, keeps it for the estate admin, and, since an unborn incarnation held no grant and the bucket holds only what the
    create itself wrote (its directory catalog's row), releases the claim; the next tick makes git's ``wh-a``."""
    alice, _erin = _admins(estate, "alice", "erin")
    _orphan_by_a_create_held_across_its_projects_delete(estate, monkeypatch, alice)
    written = _keys(estate, "wh-a-bucket")
    assert written and all(key.startswith("__manifest/") for key in written), written

    estate.declare(projects=[{"id": PROJECT, "admins": [f"{IDP}~erin"]}], warehouses=[{"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}])
    refused = estate.tick()
    assert [f for f in refused.failed if f.startswith("warehouse:wh-a")], refused
    # The boot backfill writes no edge from the record onto the acme erin holds, and erin's acme lists no such warehouse.
    assert estate.client.portal is not None
    estate.client.portal.call(boot.converge_at_boot, cast(FastAPI, estate.client.app))
    listed = estate.client.get(f"/management/v1/projects/{PROJECT}", headers=estate.bearer("erin")).json()
    assert ("project:acme", "project", "warehouse:wh-a") not in estate.store.tuples() and listed.get("warehouses") == [], listed
    ended = _converge(estate, bound=timedelta(0))
    settings = _settings(estate)
    kept = warehouses.get_warehouse_record(settings.registry_root, settings.storage_options(), "wh-a")

    converged = estate.tick()

    made = estate.record("wh-a") or {}
    assert (converged.failed, made.get(PROJECT_INCARNATION), made.get("managed_by")) == ([], _project_incarnation(estate), "git"), converged
    assert ("project:acme", "project", "warehouse:wh-a") in estate.store.tuples()
    assert (ended.orphans, ended.ended, ended.released) == (["warehouse:wh-a"], ["warehouse:wh-a"], ["wh-a-bucket"]), ended
    assert kept is not None and kept.get("ended_at") and kept.get("ended_reason") == "made_under_a_dead_incarnation", kept


class _Announced:
    """The control emitter, as a list of the events the doors published."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)


def test_a_born_dead_tenants_warehouse_keeps_its_bucket_until_the_estate_admin_purges_it(
    estate: Any,  # noqa: ANN401 — the conftest's Estate
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warehouse record whose project incarnation no longer stands and whose incarnation was born may hold what a door
    made in it (here a namespace): the tick reports it and keeps it, with its bucket's claim, so no tenant registers that
    bucket; the live tenant's admin is answered the missing warehouse's 404. The estate admin's purge-delete is the one
    door that ends it: refused without ``?purge_bucket=true``, then its bytes, grants, record and claim go, audited and
    announced, and the bucket is another tenant's to take (P3a)."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    alice, erin = _admins(estate, "alice", "erin")
    assert estate.client.post("/management/v1/projects", json={"id": PROJECT}, headers=alice).status_code == 200
    assert estate.client.post("/management/v1/projects", json={"id": "beta"}, headers=erin).status_code == 200
    granted = estate.client.put(f"/management/v1/projects/{PROJECT}/members", json={"user": f"{IDP}~bob", "relation": "admin"}, headers=alice)
    assert granted.status_code == 200, granted.text
    made = estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": PROJECT, "bucket": "wh-a-bucket"}, headers=alice)
    assert made.status_code == 200, made.text
    assert estate.client.post("/v1/namespace/wh-a$sales/create", json={}, headers=alice).status_code == 200
    # The record as a born dead tenant's: made under an incarnation of acme that no longer stands (`GrantsChildBirthUnfenced`
    # shows how a convergence that bore a record without a fence of its project left one).
    settings = _settings(estate)
    for key in (warehouses.record_key("wh-a"), f"{control_prefixes.WAREHOUSES}/bucket-claims/wh-a-bucket.json"):
        mutate_json(settings.registry_root, settings.storage_options(), key, lambda record: {**record, PROJECT_INCARNATION: "d" * 32})

    kept = _converge(estate, bound=timedelta(0))
    bob = estate.client.delete("/management/v1/warehouses/wh-a", params={"purge_bucket": "true", "cascade": "true"}, headers=estate.bearer("bob"))
    taken = estate.client.post("/management/v1/warehouses", json={"id": "wh-e", "project": "beta", "bucket": "wh-a-bucket"}, headers=erin)
    unpurged = estate.client.delete("/management/v1/warehouses/wh-a", params={"cascade": "true"}, headers=erin)
    assert (kept.orphans, kept.ended, kept.released) == (["warehouse:wh-a"], [], []), kept
    assert [bob.status_code, taken.status_code, (unpurged.status_code, unpurged.json()["code"])] == [404, 409, (400, 13)], (bob.text, taken.text, unpurged.text)

    announced = _Announced()
    estate.client.app.dependency_overrides[get_control_emitter] = lambda: announced
    purged = estate.client.delete("/management/v1/warehouses/wh-a", params={"purge_bucket": "true", "cascade": "true"}, headers=erin)
    estate.client.app.dependency_overrides.pop(get_control_emitter)

    assert (purged.status_code, purged.json().get("bucket_purged")) == (200, True), purged.text
    so = settings.storage_options()
    claim = warehouses.bucket_claim(settings.registry_root, so, bucket="wh-a-bucket")
    left = [t for t in estate.store.tuples() if "warehouse:wh-a" in (t[0], t[2]) and t[1] != fga.REVISION]
    assert (warehouses.get_warehouse_record(settings.registry_root, so, "wh-a"), claim, left) == (None, None, [])
    rows = [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__.get("audit.action") == AuditAction.DEAD_TENANT_PURGE.value]
    events = [e for e in announced.events if e.action == "warehouse_deleted"]
    assert [(r["audit.subject"], r["audit.resource"], r["audit.bucket"]) for r in rows] == [(f"user:{IDP}~erin", "warehouse:wh-a", "wh-a-bucket")], rows
    assert [e.extra.get("dead_tenant") for e in events] == [True], events
    retaken = estate.client.post("/management/v1/warehouses", json={"id": "wh-e", "project": "beta", "bucket": "wh-a-bucket"}, headers=erin)
    assert retaken.status_code == 200, retaken.text
