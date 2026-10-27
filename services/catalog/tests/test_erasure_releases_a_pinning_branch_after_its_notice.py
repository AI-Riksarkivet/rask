"""A settled erasure tells each person, then releases what pins its residual at the deadline ([[LH-178]], ADR 0138).

A verification settles an obligation only once every head is clean ([[LH-493]], ADR 0163), so what can still pin
a residual is a tag over a version whose subject rows a deletion vector hides: the version reads clean, so the tag
is kept as a reproducibility pointer, and it keeps the subject's bytes. The notice goes to the obligation's
requester and the table's owners, and the release tick deletes the tag at the deadline.

A release record may name branches too, and the tick's contract for them stands: Lance refuses to delete a branch
another branch is cut from, so it takes the subtree leaf-first and recreates it top-down, ``work`` from main's head
and ``deeper`` from ``work`` (ADR 0138, ``a1b_child_branch_release.py`` on branch
``prototype/lance-erasure-and-commit-door``), and a tick cut short is finished at the reference it recorded.

Driven through the erasure doors and the release's cron door on one app, against real pylance on ``tmp_path``: the
claim is the whole path a person sees, from the notice to an erasure that completes.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, LanceNamespace
from tables_by_name import open_by_name

from catalog.api import erasure_release as release_door
from catalog.api.dependencies import get_clock, get_control_emitter, get_namespace, get_storage_options
from catalog.api.security import authenticate
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings, get_settings
from catalog.core.identifiers import with_minted_uuid
from catalog.services import erasure_release, table_claims
from catalog.services.dataplane import create_table, read_arrow_body
from catalog.services.erasure import Pin
from service_kit.control_events import CatalogControlEvent
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from service_kit.lakehouse.base_registry import BaseRegistry
from service_kit.lakehouse.history_fences import HistoryWork, TableHistory
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.ns_errors import install_problem_handlers


_PREDICATE = "pii = 'alice'"
_TABLE_ID = ["db", "subjects"]
_BINDING = "catalog-erasure-release-cron"
_NOTICE = "erasure_release_scheduled"


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _created(root: Path) -> LanceNamespace:
    """``subjects`` at v1, alice and bob, through the catalog's create path, its claim on the control root beside ``root``."""
    ns = lance_namespace.connect("dir", {"root": str(root)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _rows("alice", "bob").schema) as writer:
        writer.write_table(_rows("alice", "bob"))
    ns.create_namespace(CreateNamespaceRequest(id=_TABLE_ID[:-1], properties=with_minted_uuid(None)))
    body = read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=1 << 20)
    registry = BaseRegistry(control_root=str(root.parent / "control"), storage_options={})
    create_table(ns, {}, _TABLE_ID, body, root=str(root), mode="create", registry=registry, holder="db$subjects")
    return ns


def _table_uuid(ns: LanceNamespace, tmp_path: Path) -> str:
    """The uuid the table's claim records, which a release names it by (ADR 0173 §8)."""
    return table_claims.resolve(ns, ClaimStore(control_root=str(tmp_path / "control")), _TABLE_ID).require_uuid()


def _tagged_over_a_plain_delete(root: Path) -> LanceNamespace:
    """A writer's plain delete hides alice at v2, which a run's tag ``trained`` names; v3 adds carol."""
    ns = _created(root)
    open_by_name(ns, {}, _TABLE_ID).delete(_PREDICATE)
    open_by_name(ns, {}, _TABLE_ID).tags.create("trained", 2)
    open_by_name(ns, {}, _TABLE_ID).insert(_rows("carol"))
    return ns


def _pinned_subtree(root: Path) -> LanceNamespace:
    """main v2 adds carol; ``work`` cut from main v2 gains eve at v3; ``deeper`` cut from (work, 3), tagged ``t1``."""
    ns = _created(root)
    open_by_name(ns, {}, _TABLE_ID).insert(_rows("carol"))
    lance.write_dataset(_rows("eve"), open_by_name(ns, {}, _TABLE_ID).create_branch("work", 2), mode="append")
    open_by_name(ns, {}, _TABLE_ID).create_branch("deeper", ("work", 3))
    open_by_name(ns, {}, _TABLE_ID).tags.create("t1", ("deeper", 3))
    return ns


class _Recorder:
    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@contextmanager
def _catalog(ns: LanceNamespace, tmp_path: Path, *, grace_days: int, emitter: _Recorder, clock: _Clock) -> Generator[TestClient]:
    settings = Settings(
        LANCE_S3_ACCESS_KEY_ID="k",
        LANCE_S3_SECRET_ACCESS_KEY="s",
        LANCE_CONTROL_ROOT=str(tmp_path / "control"),
        LANCE_ERASURE_RELEASE_GRACE_DAYS=grace_days,
        LANCE_ERASURE_RELEASE_BINDING_NAME=_BINDING,
    )
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    release_door.mount_erasure_release(application, _BINDING)

    table = f"table:{_table_uuid(ns, tmp_path)}"

    async def _owners(obj: str) -> list[str]:
        # The owners of the table's uuid, which its grants are held by (ADR 0173 §8); any other object has none.
        return ["user:dex~olivia", "user:dex~alice%40corp"] if obj == table else []

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: ns
    application.dependency_overrides[get_storage_options] = lambda: {}
    application.dependency_overrides[authenticate] = lambda: PersonPrincipal(
        principal=Principal(idp_id="dex", subject="alice@corp"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    )
    application.dependency_overrides[get_control_emitter] = lambda: emitter
    application.dependency_overrides[door.get_table_owners] = lambda: _owners
    application.dependency_overrides[get_clock] = lambda: clock
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def _branches(ns: LanceNamespace) -> dict[str, tuple[str | None, int]]:
    return {name: (meta["parent_branch"], meta["parent_version"]) for name, meta in open_by_name(ns, {}, _TABLE_ID).branches.list().items()}


def _record(client: TestClient) -> str:
    response = client.post("/management/v1/table/db$subjects/erasure", json={"predicate": _PREDICATE, "retain_days": 0})
    assert response.status_code == 202, response.text
    return str(response.json()["obligation_id"])


def _verify(client: TestClient, obligation_id: str) -> dict[str, Any]:
    response = client.post(f"/management/v1/table/db$subjects/erasure/{obligation_id}/verify", json={"predicate": _PREDICATE})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_settled_erasure_tells_each_person_then_releases_its_pin_at_the_deadline(tmp_path: Path, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()

    with _catalog(ns, tmp_path, grace_days=7, emitter=emitter, clock=clock) as client:
        obligation = _record(client)
        erasure_job(open_by_name(ns, {}, _TABLE_ID).uri, _PREDICATE).run()
        verified = _verify(client, obligation)
        refs = ["tag:trained"]
        assert ([pin["ref"] for pin in verified["report"]["pinned_by"]], verified["obligation"]["state"]) == (refs, "released")
        release = verified["report"]["release"]
        due = datetime.fromisoformat(release["due_at"])
        assert (release["refs"], release["notified"], due) == (refs, ["user:dex~alice%40corp", "user:dex~olivia"], clock.now + timedelta(days=7))
        notices = [event for event in emitter.events if event.action == _NOTICE]
        table = f"table:{_table_uuid(ns, tmp_path)}"
        assert [(e.object_id, e.object_name, e.actor, e.extra["subject"], e.extra["refs"], datetime.fromisoformat(e.extra["due_at"])) for e in notices] == [
            (table, "db$subjects", "user:dex~alice@corp", subject, refs, due) for subject in ("user:dex~alice%40corp", "user:dex~olivia")
        ]

        clock.now = due - timedelta(seconds=1)
        assert client.post(f"/{_BINDING}").status_code == 200
        assert "trained" in open_by_name(ns, {}, _TABLE_ID).tags.list(), "nothing is released before the deadline"

        clock.now = due + timedelta(seconds=1)
        assert client.post(f"/{_BINDING}").status_code == 200
        again = _verify(client, _record(client))

    assert (again["report"]["complete"], again["report"]["residual_versions"], again["report"]["release"]) == (True, [], None), again["report"]["surfaces"]
    assert "trained" not in open_by_name(ns, {}, _TABLE_ID).tags.list()


@pytest.mark.parametrize(
    ("scheduled", "settles"),
    [pytest.param("tag:trained", True, id="it-names-every-pin"), pytest.param("tag:other", False, id="it-misses-a-pin")],
)
def test_a_release_an_earlier_verification_scheduled_settles_the_obligation_only_when_it_names_every_pin(
    tmp_path: Path,
    erasure_job: Any,  # noqa: ANN401 — conftest.ErasureJob
    scheduled: str,
    settles: bool,
) -> None:
    """A verification that scheduled the release under the obligation's id and did not get to settle it leaves the
    record for the next one, whose people were told then. A pin the record does not name would stay pinned with the
    obligation released, so that verification leaves it open and names the pin."""
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    with _catalog(ns, tmp_path, grace_days=7, emitter=emitter, clock=clock) as client:
        obligation = _record(client)
        erasure_job(open_by_name(ns, {}, _TABLE_ID).uri, _PREDICATE).run()
        earlier = erasure_release.new_release(
            [Pin(ref=scheduled, holds="main@2")],
            release_id=obligation,
            table="db$subjects",
            table_uuid=_table_uuid(ns, tmp_path),
            location=open_by_name(ns, {}, _TABLE_ID).uri,
            requested_by="user:dex~alice",
            notified=["user:dex~alice", "user:dex~olivia"],
            due_at=clock.now + timedelta(days=7),
        )
        erasure_release.schedule(str(tmp_path / "control"), {}, earlier)
        verified = _verify(client, obligation)

    release = [s["detail"] for s in verified["report"]["surfaces"] if s["surface"] == "release"]
    assert (verified["obligation"]["state"], [event.action for event in emitter.events]) == ("released" if settles else "open", [])
    assert (verified["report"]["release"] is not None, bool(release and "tag:trained" in release[0])) == (settles, not settles), release


def _dies_once(monkeypatch: pytest.MonkeyPatch, owner: type, name: str, *, after: int) -> None:
    """Make ``owner.name`` raise once, on the call after ``after`` successful ones, as a store going away mid-release would."""
    real, calls = getattr(owner, name), iter(range(after + 1))

    def _flaky(self: object, *args: Any, **kwargs: Any) -> Any:
        if next(calls, None) == after:
            raise OSError("the store went away mid-release")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(owner, name, _flaky)


@pytest.mark.parametrize("dies_in", ["recreate", "deletes"])
def test_a_release_cut_short_is_finished_at_the_reference_it_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dies_in: str) -> None:
    """The release records where each branch comes back before it deletes one, so a tick that dies is finished
    by the next at that reference, not at wherever main moved to. Dying between its deletes, ``late`` is cut
    from ``work`` before the next tick: Lance would refuse ``work``'s delete while it stands, so it goes with
    the subtree and comes back from ``work``."""
    ns = _pinned_subtree(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    pins = [Pin(ref="tag:t1", holds="deeper@3"), Pin(ref="branch:deeper", holds="work@3"), Pin(ref="branch:work", holds="main@2")]
    release = erasure_release.new_release(
        pins,
        release_id="0" * 32,
        table="db$subjects",
        table_uuid=_table_uuid(ns, tmp_path),
        location=open_by_name(ns, {}, _TABLE_ID).uri,
        requested_by="user:dex~alice",
        notified=["user:dex~alice"],
        due_at=clock.now,
    )
    erasure_release.schedule(str(tmp_path / "control"), {}, release)

    with _catalog(ns, tmp_path, grace_days=0, emitter=emitter, clock=clock) as client:
        recorded = open_by_name(ns, {}, _TABLE_ID).version
        if dies_in == "recreate":
            _dies_once(monkeypatch, lance.LanceDataset, "create_branch", after=0)
        else:
            _dies_once(monkeypatch, type(open_by_name(ns, {}, _TABLE_ID).branches), "delete", after=1)
        client.post(f"/{_BINDING}")
        expected = {"work": (None, recorded), "deeper": ("work", recorded)}
        if dies_in == "deletes":
            assert set(_branches(ns)) == {"work"}, "the first tick deleted the leaf and died before its parent"
            open_by_name(ns, {}, _TABLE_ID).create_branch("late", ("work", None))
            expected["late"] = ("work", recorded)
        else:
            assert _branches(ns) == {}, "the first tick deleted the subtree and died before recreating it"
        open_by_name(ns, {}, _TABLE_ID).insert(_rows("zed"))
        assert client.post(f"/{_BINDING}").status_code == 200

    assert _branches(ns) == expected


def test_a_release_waits_while_a_reclaim_holds_the_tables_history(tmp_path: Path) -> None:
    """A release recreates branches at the versions it recorded, and Lance's reclaim reads the refs once, so a branch
    recreated while a reclaim runs could be cut from a version it deletes ([[LH-308]]). The tick changes no ref and
    leaves the release for a later one."""
    ns = _pinned_subtree(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    pins = [Pin(ref="tag:t1", holds="deeper@3"), Pin(ref="branch:deeper", holds="work@3"), Pin(ref="branch:work", holds="main@2")]
    table_uuid = _table_uuid(ns, tmp_path)
    release = erasure_release.new_release(
        pins,
        release_id="1" * 32,
        table="db$subjects",
        table_uuid=table_uuid,
        location=open_by_name(ns, {}, _TABLE_ID).uri,
        requested_by="user:dex~alice",
        notified=["user:dex~alice"],
        due_at=clock.now,
    )
    erasure_release.schedule(str(tmp_path / "control"), {}, release)
    branches, tags = _branches(ns), open_by_name(ns, {}, _TABLE_ID).tags.list()
    TableHistory(control_root=str(tmp_path / "control"), storage_options={}, table_uuid=table_uuid).take(HistoryWork.RECLAIM)

    with _catalog(ns, tmp_path, grace_days=0, emitter=emitter, clock=clock) as client:
        tick = client.post(f"/{_BINDING}")

    assert tick.status_code == 200, tick.text
    assert (_branches(ns), open_by_name(ns, {}, _TABLE_ID).tags.list()) == (branches, tags), "the release changed refs while a reclaim held the history"
    assert erasure_release.scheduled(str(tmp_path / "control"), {}, release.release_id) is not None, "the release did not wait for a later tick"
