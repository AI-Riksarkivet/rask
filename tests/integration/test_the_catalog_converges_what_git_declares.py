"""The catalog converges the projects, warehouses and service grants git declares, and the estate's own grants ([[XC-213]], [[XC-011]], ADR 0206).

The declaration is the file the chart renders from its values and the catalog mounts. A tick creates what it names and
corrects what drifted, through the functions the doors call, so an install from git holds its tenants with no call to
any door. Driven on the deployed shape: the real app with authorization on (a loopback Dex, a real OpenFGA store of the
test's own) over an S3 store, the tick run on the app's own event loop as the catalog's timer runs it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, cast

import pytest
from fastapi import FastAPI
from lance_namespace import ServiceUnavailableError

from catalog import main as boot
from catalog.api import fga_deps
from catalog.api.v1.endpoints import warehouses as warehouse_doors
from catalog.services import declaration, projects, tenancy, warehouses
from lakehouse.governed import fga
from lakehouse.governed.audit import AUDIT_LOGGER, AuditAction, AuditOutcome
from lakehouse.governed.tenancy import Kind


CONNECTOR, IDP = "local", "dex"
#: A pipeline's service account: the kind of principal a declaration grants, never a person.
PIPELINE = "kubernetes~system:serviceaccount:default:pipeline"
#: One of the release's own accounts (``LANCE_OWN_ACCOUNTS``), whose machine roles on the estate converge to git's.
MAINTENANCE = "kubernetes~system:serviceaccount:default:rask-sa-maintenance"
ROOT, ESTATE_WAREHOUSE = "estate:rask", "warehouse:lance_catalog"
WRITE, DELETE = AuditAction.ACCESS_TUPLE_WRITE, AuditAction.ACCESS_TUPLE_DELETE
PROBLEM = "application/problem+json"


def _project(project_id: str, *, admin: str = "alice") -> dict[str, Any]:
    return {"id": project_id, "admins": [f"{IDP}~{admin}"]}


def _warehouse(warehouse_id: str, project: str, **spec: Any) -> dict[str, Any]:  # noqa: ANN401 — a declaration's own fields
    return {"id": warehouse_id, "project": project, "bucket": f"{warehouse_id}-bucket", **spec}


def _grant(obj: str, relation: str) -> dict[str, str]:
    return {"object": obj, "relation": relation, "principal": PIPELINE}


def _creation(estate: Any, kind: Kind, object_id: str) -> str:  # noqa: ANN401 — the conftest's Estate
    """The object OpenFGA names the live creation of ``kind`` ``object_id`` by (ADR 0245)."""
    settings = estate.client.app.state.settings
    found = tenancy.resolve(settings.registry_root, settings.storage_options(), kind, object_id)
    assert found is not None, f"no live {kind} {object_id!r}"
    return found.obj


def test_a_tick_makes_every_declared_project_warehouse_and_service_grant_with_no_door_called(estate: Any) -> None:
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
    assert (fga.user_object(PIPELINE), "writer", _creation(estate, "warehouse", "acme")) in estate.store.tuples()


def _declared_acme(estate: Any) -> None:
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
def test_a_door_refuses_to_change_what_git_declares(estate: Any, caplog: pytest.LogCaptureFixture, method: str, path: str, body: dict[str, Any] | None) -> None:
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


def test_a_stack_that_authenticates_nobody_still_refuses_a_re_post_of_a_declared_warehouse(open_estate: Any) -> None:
    _declared_acme(open_estate)
    before = open_estate.record("acme")

    refused = open_estate.client.post("/management/v1/warehouses", json={"id": "acme", "project": "acme", "bucket": "acme-bucket", "protected": True})

    assert (refused.status_code, refused.json()["code"], open_estate.record("acme")) == (403, 15, before), refused.text


def test_a_warehouse_whose_establish_failed_is_completed_by_the_next_tick_and_then_left_alone(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert [(tick.converged, [failure.partition(":")[0] + ":" + failure.split(":")[1] for failure in tick.failed]) for tick in ticks] == [
        (["project:acme"], ["warehouse:acme"]),
        (["warehouse:acme"], []),
        ([], []),
    ]
    assert (made.status_code, (estate.record("acme") or {}).get("managed_by")) == (200, "git"), made.text


def test_a_warehouse_git_declares_in_another_project_than_it_stands_in_is_left_as_it_is(estate: Any) -> None:
    projects = [_project("acme"), _project("research", admin="bob")]
    estate.declare(projects=projects, warehouses=[_warehouse("shared", "acme")])
    estate.tick()
    # What the declaration owns, without the tick's own bookkeeping (`born`, ADR 0241 §9 P5).
    declared = lambda: {k: v for k, v in (estate.record("shared") or {}).items() if k != "born"}  # noqa: E731
    before = declared()
    estate.declare(projects=projects, warehouses=[_warehouse("shared", "research", active=False, protected=True)])

    answer = estate.tick()

    assert (answer.converged, [failure.split(": ", 1)[0] for failure in answer.failed], declared()) == ([], ["warehouse:shared"], before)


def test_a_paged_root_listing_shows_every_warehouse_the_caller_sees(estate: Any) -> None:
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


def test_a_deactivated_warehouse_is_told_only_to_a_caller_its_door_would_admit(estate: Any) -> None:
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


def _declared_writes(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str, str, str]]:
    """Each tuple the declaration wrote or deleted, as the audit trail names it: action, grantee, relation, object."""
    rows = [r.__dict__ for r in caplog.records if r.name == AUDIT_LOGGER and r.__dict__.get("audit.subject") == "system:declaration"]
    return sorted(
        (row["audit.action"], row["audit.grantee"], row["audit.relation"], row["audit.resource"]) for row in rows if row["audit.action"] in (WRITE, DELETE)
    )


def test_a_fresh_install_grants_the_estate_admin_git_declares_and_audits_each_tuple(estate: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    estate.declare(estate={"admins": [f"{IDP}~alice"]})

    estate.tick()

    alice = fga.user_object(f"{IDP}~alice")
    assert _declared_writes(caplog) == [(WRITE, alice, "admin", ROOT), (WRITE, alice, "owner", ESTATE_WAREHOUSE)]
    assert {(alice, "admin", ROOT), (alice, "owner", ESTATE_WAREHOUSE)} <= estate.store.tuples()


def test_a_tick_on_an_estate_as_declared_changes_nothing(estate: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    estate.declare(
        projects=[_project("acme")],
        warehouses=[_warehouse("acme", "acme")],
        estate={"admins": [f"{IDP}~alice"], "roles": [{"relation": "reclaimer", "principal": MAINTENANCE}]},
    )
    estate.tick()
    held = estate.store.tuples()
    caplog.clear()

    answer = estate.tick()

    assert (answer.converged, answer.grants_written, answer.grants_revoked, answer.failed) == ([], 0, 0, [])
    assert (_declared_writes(caplog), estate.store.tuples()) == ([], held)


def test_another_estate_admin_in_git_moves_both_grants_and_the_audit_trail_names_each(estate: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    estate.declare(estate={"admins": [f"{IDP}~alice"]})
    estate.tick()
    caplog.clear()
    estate.declare(estate={"admins": [f"{IDP}~carol"]})

    estate.tick()

    alice, carol = fga.user_object(f"{IDP}~alice"), fga.user_object(f"{IDP}~carol")
    assert _declared_writes(caplog) == [
        (DELETE, alice, "admin", ROOT),
        (DELETE, alice, "owner", ESTATE_WAREHOUSE),
        (WRITE, carol, "admin", ROOT),
        (WRITE, carol, "owner", ESTATE_WAREHOUSE),
    ]
    assert {(user, relation, obj) for user, relation, obj in estate.store.tuples() if obj in (ROOT, ESTATE_WAREHOUSE) and relation in ("admin", "owner")} == {
        (carol, "admin", ROOT),
        (carol, "owner", ESTATE_WAREHOUSE),
    }


def test_an_estate_admin_whose_successor_could_not_be_granted_keeps_the_estate(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store refuses carol's grant on the tick that moves the estate to her: alice keeps both rungs until carol
    holds them, so the estate never stands without an administrator between ticks."""
    estate.declare(estate={"admins": [f"{IDP}~alice"]})
    estate.tick()
    alice, carol = fga.user_object(f"{IDP}~alice"), fga.user_object(f"{IDP}~carol")
    write = fga.commit_unless_changed

    async def refusing_carol(client: Any, *, writes: list[fga.ClientTuple], **kwargs: Any) -> Any:
        if any(t.user == carol for t in writes):
            raise ServiceUnavailableError("the store did not answer")
        return await write(client, writes=writes, **kwargs)

    monkeypatch.setattr(fga, "commit_unless_changed", refusing_carol)
    estate.declare(estate={"admins": [f"{IDP}~carol"]})

    answer = estate.tick()

    held = {(user, relation, obj) for user, relation, obj in estate.store.tuples() if obj in (ROOT, ESTATE_WAREHOUSE) and relation in ("admin", "owner")}
    assert (answer.grants_revoked, held) == (0, {(alice, "admin", ROOT), (alice, "owner", ESTATE_WAREHOUSE)})


def test_a_machine_role_git_stopped_declaring_is_taken_from_the_release_and_one_granted_elsewhere_stands(estate: Any) -> None:
    """The release's own account held ``reconciler`` before any ledger did, as an earlier install writes it; a pipeline
    outside the release was made a ``decider`` at a door. Git declares neither, so only the release's role goes."""
    maintenance, pipeline = fga.user_object(MAINTENANCE), fga.user_object(PIPELINE)
    estate.store.write((maintenance, "reconciler", ROOT), (maintenance, "reclaimer", ROOT), (pipeline, "decider", ROOT))
    estate.declare(estate={"roles": [{"relation": "reclaimer", "principal": MAINTENANCE}]})

    estate.tick()

    held = {(user, relation) for user, relation, obj in estate.store.tuples() if obj == ROOT}
    assert held == {(maintenance, "reclaimer"), (pipeline, "decider")}


@pytest.mark.parametrize(
    "granted",
    [
        pytest.param({"grants": [{"object": "warehouse:../acme", "relation": "writer", "principal": PIPELINE}]}, id="object-id-no-control-id"),
        pytest.param({"grants": [{"object": "warehouse:acme", "relation": "writer", "principal": f"{IDP}~alice"}]}, id="a-person"),
        pytest.param({"estate": {"admins": [PIPELINE]}}, id="a-machine-administers-the-estate"),
        pytest.param({"estate": {"roles": [{"relation": "reclaimer", "principal": f"{IDP}~alice"}]}}, id="a-person-holds-a-machine-role"),
        pytest.param({"estate": {"roles": [{"relation": "admin", "principal": MAINTENANCE}]}}, id="a-role-that-is-no-machine-role"),
    ],
)
def test_a_declaration_granting_what_it_may_not_converges_nothing(estate: Any, granted: dict[str, Any]) -> None:
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("acme", "acme")], **granted)

    answer = estate.tick()

    assert (answer.converged, answer.failed, estate.record("acme")) == ([], ["declaration"], None)


def test_a_tick_corrects_a_declared_warehouse_that_drifted(estate: Any) -> None:
    """The drift is planted on the record itself, as nothing but a write past the doors can make it."""
    _declared_acme(estate)
    settings = estate.client.app.state.settings
    warehouses.set_warehouse_status(settings.registry_root, settings.storage_options(), "acme", "deactivated", uuid=(estate.record("acme") or {})["uuid"])

    answer = estate.tick()

    assert (answer.converged, (estate.record("acme") or {}).get("status")) == (["warehouse:acme"], "active")


@pytest.mark.parametrize("granted", ["before-git-declares-it", "as-the-tick-writes-it", "after-a-tick-died-between-its-intent-and-its-write"])
def test_a_grant_removed_from_git_is_revoked_and_one_made_at_a_door_stands(estate: Any, monkeypatch: pytest.MonkeyPatch, granted: str) -> None:
    """The catalog revokes only a tuple it wrote from the declaration: the reader grant here was made at the warehouse's
    access door, before git ever declared it, between the tick's last read of the store and its write (ADR 0241, the
    model's `GrantsConvergeAdopts`), or after a tick died between recording its intent and writing (`GrantsConvergeDies`),
    so removing it from git takes nothing."""
    _declared_acme(estate)

    def _at_the_door() -> None:
        at_the_door = estate.client.post(
            "/management/v1/warehouse/acme/access/grant", json={"user": PIPELINE, "relation": "reader"}, headers=estate.bearer("alice")
        )
        assert at_the_door.status_code == 200, at_the_door.text

    declared = [_grant("warehouse:acme", "reader"), _grant("warehouse:acme", "writer")]
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("acme", "acme")], grants=declared)
    if granted == "before-git-declares-it":
        _at_the_door()
        estate.tick()
    elif granted == "as-the-tick-writes-it":
        recorded, release, real_record = threading.Event(), threading.Event(), declaration.record_intent

        def _record(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401 — record_intent's own arguments
            made = real_record(*args, **kwargs)
            if args[-1].relation == "reader" and not recorded.is_set():
                recorded.set()
                assert release.wait(30), "the held tick was never released"
            return made

        monkeypatch.setattr(declaration, "record_intent", _record)
        ticking = threading.Thread(target=estate.tick)
        ticking.start()
        assert recorded.wait(30), "the tick never recorded the reader grant"
        _at_the_door()
        release.set()
        ticking.join(30)
    else:
        real_write = fga.write_evidenced

        async def _dies(client: Any, t: fga.ClientTuple, *args: Any, **kwargs: Any) -> bool:  # noqa: ANN401 — write_evidenced's own arguments
            if t.relation == "reader":
                raise ServiceUnavailableError("the tick's process died before its write")
            return await real_write(client, t, *args, **kwargs)

        monkeypatch.setattr(fga, "write_evidenced", _dies)
        estate.tick()
        monkeypatch.setattr(fga, "write_evidenced", real_write)
        _at_the_door()
        estate.tick()
    estate.declare(projects=[_project("acme")], warehouses=[_warehouse("acme", "acme")])

    estate.tick()

    held = {relation for user, relation, obj in estate.store.tuples() if user == fga.user_object(PIPELINE) and obj == _creation(estate, "warehouse", "acme")}
    assert held == {"reader"}


@pytest.mark.parametrize("converged_by", ["boot", "tick"])
def test_a_project_whose_create_died_before_its_seed_is_finished_by_the_catalog(estate: Any, monkeypatch: pytest.MonkeyPatch, converged_by: str) -> None:
    """A create can die between its record and its flip (ADR 0245, `GrantsCreatingUnsettled`): the record stands
    `creating`, which no door resolves. The catalog finishes it at boot and on every converge tick from what the record
    names, its admin's seed on its uuid and then `live`, with no hand-run step, and the doors work."""
    estate.store.write((fga.user_object(f"{IDP}~alice"), "admin", ROOT))
    alice, members = estate.bearer("alice"), "/management/v1/projects/acme/members"
    real_seed = fga_deps.seed_project_admin

    async def _seed_lost(*_args: Any, **_kwargs: Any) -> bool:  # noqa: ANN401 — seed_project_admin's own arguments
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(fga_deps, "seed_project_admin", _seed_lost)
    assert estate.client.post("/management/v1/projects", json={"id": "acme"}, headers=alice).status_code == 503
    monkeypatch.setattr(fga_deps, "seed_project_admin", real_seed)
    settings = estate.client.app.state.settings
    record = projects.get_project_record(settings.registry_root, settings.storage_options(), "acme")
    assert record is not None and record["state"] == "creating", record
    # Gated as an object nobody holds anything on, so the caller learns nothing of a record no door resolves.
    assert estate.client.get(members, headers=alice).status_code == 403, "a door resolved a creating record"

    app = cast(FastAPI, estate.client.app)
    assert estate.client.portal is not None
    if converged_by == "boot":
        estate.client.portal.call(boot.converge_at_boot, app)
    else:
        assert estate.tick().failed == []
    held = estate.store.tuples()
    assert (fga.user_object(f"{IDP}~alice"), "admin", f"project:{record['uuid']}") in held, "the convergence wrote no seed"
    granted = estate.client.put(members, json={"user": f"{IDP}~bob", "relation": "member"}, headers=alice)
    last = estate.client.request("DELETE", members, json={"user": f"{IDP}~alice", "relation": "admin"}, headers=alice)
    assert (granted.status_code, last.status_code, last.json()["code"]) == (200, 409, 14), (granted.text, last.text)


class _Gate:
    def __init__(self) -> None:
        self.arrived, self.release = threading.Event(), threading.Event()

    async def hold(self) -> None:
        if not self.arrived.is_set():
            self.arrived.set()
            assert await asyncio.to_thread(self.release.wait, 30), "the held step was never released"


def test_a_warehouse_made_across_its_projects_delete_lands_in_no_tenant_made_again_under_the_id(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """alice makes warehouse ``wh-a`` in her project ``acme`` and is held in its store's probe while acme is deleted and
    erin makes acme again (ADR 0245, `GrantsChildReadFirst`): its record, written after the delete, names acme's dead
    creation, and its resolve after the record finds another, so it ends its record and answers 404. Erin's acme holds no
    warehouse of alice's, and no record of it stands."""
    for person in ("alice", "erin"):
        estate.store.write((fga.user_object(f"{IDP}~{person}"), "admin", ROOT))
    alice, erin = estate.bearer("alice"), estate.bearer("erin")
    assert estate.client.post("/management/v1/projects", json={"id": "acme"}, headers=alice).status_code == 200
    made = {"id": "wh-a", "project": "acme", "bucket": "wh-a-bucket"}
    gate, real_probe = _Gate(), warehouse_doors._require_a_conforming_store

    async def _probe(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — the probe's own arguments
        await gate.hold()
        await real_probe(*args, **kwargs)

    monkeypatch.setattr(warehouse_doors, "_require_a_conforming_store", _probe)
    answered: list[int] = []
    making = threading.Thread(target=lambda: answered.append(estate.client.post("/management/v1/warehouses", json=made, headers=alice).status_code))
    making.start()
    assert gate.arrived.wait(30), "alice's create never reached the held step"
    steps = [
        estate.client.delete("/management/v1/projects/acme", headers=alice),
        estate.client.post("/management/v1/projects", json={"id": "acme"}, headers=erin),
    ]
    gate.release.set()
    making.join(30)

    assert [step.status_code for step in steps] == [200, 200], [step.text for step in steps]
    assert (answered, estate.record("wh-a")) == ([404], None), "alice's warehouse is recorded in the project erin made"
    assert not [t for t in estate.store.tuples() if t[0] == _creation(estate, "project", "acme") and t[1] == "project"], "erin's acme holds a warehouse edge"


def test_a_namespace_made_in_a_warehouse_across_its_delete_lands_in_no_warehouse_made_again_under_the_id(estate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """alice's namespace ``wh-a$sales`` is created and held before its seed while ``wh-a`` is deleted with what it holds
    and erin makes ``wh-a`` again in a project of hers (ADR 0245, `ChildUnderLiveParent` with Cascade): its ``parent``
    edge names the creation of ``wh-a`` the router resolved, and once written the door resolves the warehouse again,
    finds another creation, takes the namespace back and answers 404, so it lands in no warehouse made again under the id."""
    for person in ("alice", "erin"):
        estate.store.write((fga.user_object(f"{IDP}~{person}"), "admin", ROOT))
    alice, erin = estate.bearer("alice"), estate.bearer("erin")
    for project, by in (("acme", alice), ("beta", erin)):
        assert estate.client.post("/management/v1/projects", json={"id": project}, headers=by).status_code == 200
    made = estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme", "bucket": "wh-a-bucket"}, headers=alice)
    assert made.status_code == 200, made.text
    gate, real_seed = _Gate(), fga_deps.seed_ownership

    async def _seed(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — seed_ownership's own arguments
        await gate.hold()
        await real_seed(*args, **kwargs)

    monkeypatch.setattr(fga_deps, "seed_ownership", _seed)
    answered: list[int] = []
    making = threading.Thread(target=lambda: answered.append(estate.client.post("/v1/namespace/wh-a$sales/create", json={}, headers=alice).status_code))
    making.start()
    assert gate.arrived.wait(30), "alice's namespace never reached its seed"
    steps = [
        estate.client.delete("/management/v1/warehouses/wh-a", params={"cascade": "true"}, headers=alice),
        estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "beta", "bucket": "wh-b-bucket"}, headers=erin),
    ]
    gate.release.set()
    making.join(30)

    assert [step.status_code for step in steps] == [200, 200], [step.text for step in steps]
    erins = _creation(estate, "warehouse", "wh-a")
    edges = [t for t in estate.store.tuples() if erins in (t[0], t[2]) and t[1] in ("parent", "child")]
    assert (answered, edges) == ([404], []), f"the namespace door answered {answered}, and erin's warehouse holds {edges}"


def _released_acme(estate: Any) -> dict[str, str]:
    """An estate whose project ``acme`` and warehouse ``acme`` were declared, hold a table, and are declared no more; alice's headers."""
    alice = estate.bearer("alice")
    _declared_acme(estate)
    assert estate.client.post("/v1/namespace/acme$sales/create", json={}, headers=alice).status_code == 200
    assert estate.client.post("/v1/table/acme$sales$t/declare", json={}, headers=alice).status_code == 200
    estate.declare()
    return alice


def test_a_project_removed_from_git_is_released_and_keeps_its_data(estate: Any) -> None:
    alice = _released_acme(estate)

    answer = estate.tick()

    project = estate.client.get("/management/v1/projects/acme", headers=alice).json()
    assert (sorted(answer.released), answer.grants_revoked, answer.failed) == (["project:acme", "warehouse:acme"], 0, [])
    assert (fga.user_object(f"{IDP}~alice"), "admin", _creation(estate, "project", "acme")) in estate.store.tuples()
    assert (project["managed_by"], (estate.record("acme") or {}).get("managed_by")) == ("self", None)
    assert estate.client.get("/v1/namespace/acme$sales/table/list", headers=alice).json()["tables"] == ["t"]


def test_a_released_warehouse_is_changed_at_its_doors_again(estate: Any) -> None:
    alice = _released_acme(estate)
    estate.tick()

    deactivated = estate.client.post("/management/v1/warehouses/acme/deactivate", headers=alice)

    assert (deactivated.status_code, deactivated.json().get("status")) == (200, "deactivated"), deactivated.text
