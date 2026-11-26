"""A settled erasure tells each person, then releases what pins its residual at the deadline ([[LH-178]], ADR 0138).

A verification settles an obligation only once every head is clean ([[LH-493]], ADR 0163), so what can still pin
a residual is a tag over a version whose subject rows a deletion vector hides: the version reads clean, so the tag
is kept as a reproducibility pointer, and it keeps the subject's bytes. The notice goes to the obligation's
requester and the table's owners, and the release tick deletes the tag at the deadline.

A release record may name branches too, and the tick's contract for them stands: Lance refuses to delete a branch
another branch is cut from, so it takes the subtree leaf-first and recreates it top-down, ``work`` from main's head
and ``deeper`` from ``work`` (ADR 0138, ``a1b_child_branch_release.py`` on branch
``prototype/lance-erasure-and-commit-door``), and a tick cut short is finished at the reference it recorded.

Driven through the erasure doors on one app, with the release's tick run on that app's event loop as its timer runs it,
against real pylance on ``tmp_path``: the claim is the whole path a person sees, from the notice to an erasure that
completes.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import lance
import lance_namespace
import pytest
from erasure_rig import PREDICATE, SUBJECT, erasure_doors, rows
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, LanceNamespace
from tables_by_name import open_by_name

from catalog.api import erasure_release as release_tick
from catalog.api.dependencies import get_clock, get_control_emitter
from catalog.api.security import authenticate
from catalog.api.ticks import Tick
from catalog.api.v1.endpoints import erasure as door
from catalog.core.identifiers import with_minted_uuid
from catalog.services import erasure_release, table_claims
from catalog.services.erasure import Pin
from catalog.services.tables.creation import create_table
from lakehouse.records.base_registry import BaseRegistry
from lakehouse.records.history_fences import HistoryWork, TableHistory
from lakehouse.records.location_claims import ClaimStore
from lakehouse.storage.options import StorageOptions
from tests.callers import person
from tests.catalog_settings import catalog_settings
from tests.control_emitter_double import ControlEmitterDouble


_TABLE_ID = ["db", "subjects"]
_NOTICE = "erasure_release_scheduled"


def _created(root: Path) -> LanceNamespace:
    """``subjects`` at v1, alice and bob, through the catalog's create path, its claim on the control root beside ``root``."""
    ns = lance_namespace.connect("dir", {"root": str(root)})
    ns.create_namespace(CreateNamespaceRequest(id=_TABLE_ID[:-1], properties=with_minted_uuid(None)))
    registry = BaseRegistry(control_root=str(root.parent / "control"), storage_options={})
    create_table(ns, StorageOptions(), _TABLE_ID, table=rows(SUBJECT, "bob"), root=str(root), mode="create", registry=registry, holder="db$subjects")
    return ns


def _table_uuid(ns: LanceNamespace, tmp_path: Path) -> str:
    """The uuid the table's claim records, which a release names it by (ADR 0173 §8)."""
    return table_claims.resolve(ns, ClaimStore(control_root=str(tmp_path / "control")), _TABLE_ID).require_uuid()


def _tagged_over_a_plain_delete(root: Path) -> LanceNamespace:
    """A writer's plain delete hides alice at v2, which a run's tag ``trained`` names; v3 adds carol."""
    ns = _created(root)
    open_by_name(ns, StorageOptions(), _TABLE_ID).delete(PREDICATE)
    open_by_name(ns, StorageOptions(), _TABLE_ID).tags.create("trained", 2)
    open_by_name(ns, StorageOptions(), _TABLE_ID).insert(rows("carol"))
    return ns


def _pinned_subtree(root: Path) -> LanceNamespace:
    """main v2 adds carol; ``work`` cut from main v2 gains eve at v3; ``deeper`` cut from (work, 3), tagged ``t1``."""
    ns = _created(root)
    open_by_name(ns, StorageOptions(), _TABLE_ID).insert(rows("carol"))
    lance.write_dataset(rows("eve"), open_by_name(ns, StorageOptions(), _TABLE_ID).create_branch("work", 2), mode="append")
    open_by_name(ns, StorageOptions(), _TABLE_ID).create_branch("deeper", ("work", 3))
    open_by_name(ns, StorageOptions(), _TABLE_ID).tags.create("t1", ("deeper", 3))
    return ns


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


type Release = Callable[[], release_tick.ErasureReleaseTick]


@contextmanager
def _catalog(ns: LanceNamespace, tmp_path: Path, *, grace_days: int, emitter: ControlEmitterDouble, clock: _Clock) -> Generator[tuple[TestClient, Release]]:
    """The erasure doors on one app, and the release's tick over that app, judged by ``clock`` and announced on ``emitter``."""
    settings = catalog_settings(LANCE_CONTROL_ROOT=str(tmp_path / "control"), LANCE_ERASURE_RELEASE_GRACE_DAYS=grace_days)
    application = erasure_doors(ns, settings)

    table = f"table:{_table_uuid(ns, tmp_path)}"

    async def _owners(obj: str) -> list[str]:
        # The owners of the table's uuid, which its grants are held by (ADR 0173 §8); any other object has none.
        return ["user:dex~olivia", "user:dex~alice%40corp"] if obj == table else []

    application.dependency_overrides[authenticate] = lambda: person("alice@corp", sub="alice")
    application.dependency_overrides[get_control_emitter] = lambda: emitter
    application.dependency_overrides[door.get_table_owners] = lambda: _owners
    application.dependency_overrides[get_clock] = lambda: clock
    with TestClient(application, raise_server_exceptions=False) as client:

        def release() -> release_tick.ErasureReleaseTick:
            assert client.portal is not None, "the release ticks inside the app's lifespan"
            return client.portal.call(release_tick.tick, Tick(application, settings, clock=clock, control=cast(Any, emitter)))

        yield client, release


def _branches(ns: LanceNamespace) -> dict[str, tuple[str | None, int]]:
    return {name: (meta["parent_branch"], meta["parent_version"]) for name, meta in open_by_name(ns, StorageOptions(), _TABLE_ID).branches.list().items()}


def _record(client: TestClient) -> str:
    response = client.post("/management/v1/table/db$subjects/erasure", json={"predicate": PREDICATE, "retain_days": 0})
    assert response.status_code == 202, response.text
    return str(response.json()["obligation_id"])


def _verify(client: TestClient, obligation_id: str) -> dict[str, Any]:
    response = client.post(f"/management/v1/table/db$subjects/erasure/{obligation_id}/verify", json={"predicate": PREDICATE})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_settled_erasure_tells_each_person_then_releases_its_pin_at_the_deadline(tmp_path: Path, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = ControlEmitterDouble(), _Clock()

    with _catalog(ns, tmp_path, grace_days=7, emitter=emitter, clock=clock) as (client, run_release):
        obligation = _record(client)
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, PREDICATE).run()
        verified = _verify(client, obligation)
        refs = ["tag:trained"]
        assert ([pin["ref"] for pin in verified["report"]["pinned_by"]], verified["obligation"]["state"]) == (refs, "released")
        release = verified["report"]["release"]
        due = datetime.fromisoformat(release["due_at"])
        assert (release["refs"], release["notified"], due) == (refs, ["user:dex~alice%40corp", "user:dex~olivia"], clock.now + timedelta(days=7))
        notices = [event for event in emitter.events if event.action == _NOTICE]
        table = f"table:{_table_uuid(ns, tmp_path)}"
        assert [(e.object_id, e.object_name, e.actor, e.extra["subject"], e.extra["refs"], datetime.fromisoformat(e.extra["due_at"])) for e in notices] == [
            (table, "db$subjects", "user:dex~alice%40corp", subject, refs, due) for subject in ("user:dex~alice%40corp", "user:dex~olivia")
        ]

        clock.now = due - timedelta(seconds=1)
        run_release()
        assert "trained" in open_by_name(ns, StorageOptions(), _TABLE_ID).tags.list(), "nothing is released before the deadline"

        clock.now = due + timedelta(seconds=1)
        run_release()
        again = _verify(client, _record(client))

    assert (again["report"]["complete"], again["report"]["residual_versions"], again["report"]["release"]) == (True, [], None), again["report"]["surfaces"]
    assert "trained" not in open_by_name(ns, StorageOptions(), _TABLE_ID).tags.list()


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
    emitter, clock = ControlEmitterDouble(), _Clock()
    with _catalog(ns, tmp_path, grace_days=7, emitter=emitter, clock=clock) as (client, _run_release):
        obligation = _record(client)
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, PREDICATE).run()
        earlier = erasure_release.new_release(
            [Pin(ref=scheduled, holds="main@2")],
            release_id=obligation,
            table="db$subjects",
            table_uuid=_table_uuid(ns, tmp_path),
            location=open_by_name(ns, StorageOptions(), _TABLE_ID).uri,
            requested_by="user:dex~alice",
            notified=["user:dex~alice", "user:dex~olivia"],
            due_at=clock.now + timedelta(days=7),
        )
        erasure_release.schedule(str(tmp_path / "control"), StorageOptions(), earlier)
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
    emitter, clock = ControlEmitterDouble(), _Clock()
    pins = [Pin(ref="tag:t1", holds="deeper@3"), Pin(ref="branch:deeper", holds="work@3"), Pin(ref="branch:work", holds="main@2")]
    release = erasure_release.new_release(
        pins,
        release_id="0" * 32,
        table="db$subjects",
        table_uuid=_table_uuid(ns, tmp_path),
        location=open_by_name(ns, StorageOptions(), _TABLE_ID).uri,
        requested_by="user:dex~alice",
        notified=["user:dex~alice"],
        due_at=clock.now,
    )
    erasure_release.schedule(str(tmp_path / "control"), StorageOptions(), release)

    with _catalog(ns, tmp_path, grace_days=0, emitter=emitter, clock=clock) as (_client, run_release):
        recorded = open_by_name(ns, StorageOptions(), _TABLE_ID).version
        if dies_in == "recreate":
            _dies_once(monkeypatch, lance.LanceDataset, "create_branch", after=0)
        else:
            _dies_once(monkeypatch, type(open_by_name(ns, StorageOptions(), _TABLE_ID).branches), "delete", after=1)
        run_release()
        expected = {"work": (None, recorded), "deeper": ("work", recorded)}
        if dies_in == "deletes":
            assert set(_branches(ns)) == {"work"}, "the first tick deleted the leaf and died before its parent"
            open_by_name(ns, StorageOptions(), _TABLE_ID).create_branch("late", ("work", None))
            expected["late"] = ("work", recorded)
        else:
            assert _branches(ns) == {}, "the first tick deleted the subtree and died before recreating it"
        open_by_name(ns, StorageOptions(), _TABLE_ID).insert(rows("zed"))
        run_release()

    assert _branches(ns) == expected


def test_a_release_waits_while_a_reclaim_holds_the_tables_history(tmp_path: Path) -> None:
    """A release recreates branches at the versions it recorded, and Lance's reclaim reads the refs once, so a branch
    recreated while a reclaim runs could be cut from a version it deletes ([[LH-308]]). The tick changes no ref and
    leaves the release for a later one."""
    ns = _pinned_subtree(tmp_path / "data")
    emitter, clock = ControlEmitterDouble(), _Clock()
    pins = [Pin(ref="tag:t1", holds="deeper@3"), Pin(ref="branch:deeper", holds="work@3"), Pin(ref="branch:work", holds="main@2")]
    table_uuid = _table_uuid(ns, tmp_path)
    release = erasure_release.new_release(
        pins,
        release_id="1" * 32,
        table="db$subjects",
        table_uuid=table_uuid,
        location=open_by_name(ns, StorageOptions(), _TABLE_ID).uri,
        requested_by="user:dex~alice",
        notified=["user:dex~alice"],
        due_at=clock.now,
    )
    erasure_release.schedule(str(tmp_path / "control"), StorageOptions(), release)
    branches, tags = _branches(ns), open_by_name(ns, StorageOptions(), _TABLE_ID).tags.list()
    TableHistory(control_root=str(tmp_path / "control"), storage_options={}, table_uuid=table_uuid).take(HistoryWork.RECLAIM)

    with _catalog(ns, tmp_path, grace_days=0, emitter=emitter, clock=clock) as (_client, run_release):
        tick = run_release()

    assert tick.changed == 0, tick
    assert (_branches(ns), open_by_name(ns, StorageOptions(), _TABLE_ID).tags.list()) == (branches, tags), (
        "the release changed refs while a reclaim held the history"
    )
    assert erasure_release.scheduled(str(tmp_path / "control"), StorageOptions(), release.release_id) is not None, "the release did not wait for a later tick"


#: What a failure the catalog does not choose the words of may carry: an address, a credential.
_INTERNALS = "fga.internal:8081 refused token s3cr3t"


async def _owners_unread(obj: str) -> list[str]:
    raise RuntimeError(f"{_INTERNALS} reading {obj}")


def _schedule_refused(*_args: object) -> None:
    raise OSError(_INTERNALS)


def _plant_owners_unread(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    assert isinstance(client.app, FastAPI)
    client.app.dependency_overrides[door.get_table_owners] = lambda: _owners_unread


def _plant_release_unrecorded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    del client
    monkeypatch.setattr(erasure_release, "schedule", _schedule_refused)


def _plant_table_unopened(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    del client
    monkeypatch.setattr(door, "_open_cold", _schedule_refused)


_PLANTED = {"notice": _plant_owners_unread, "release": _plant_release_unrecorded, "verify": _plant_table_unopened}


@pytest.mark.parametrize(
    ("surface", "detail"),
    [
        pytest.param("notice", "the table's owners could not be read, so only the requester is told", id="the-owners-unread"),
        pytest.param("release", "the release of ['tag:trained'] could not be recorded, so nobody was told", id="the-release-unrecorded"),
        pytest.param("verify", "the verification could not open the table on a fresh session; nothing was verified", id="the-table-unopened"),
    ],
)
def test_a_failed_surface_is_answered_and_stored_in_the_catalogs_words(
    tmp_path: Path,
    erasure_job: Any,  # noqa: ANN401 — conftest.ErasureJob
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    detail: str,
) -> None:
    """A failure's own text goes to the log: the verification's answer and the obligation the listing serves to every
    maintainer of the table say what failed, never what the failure said."""
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    with _catalog(ns, tmp_path, grace_days=7, emitter=ControlEmitterDouble(), clock=_Clock()) as (client, _run_release):
        _PLANTED[surface](client, monkeypatch)
        obligation = _record(client)
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, PREDICATE).run()
        verified = _verify(client, obligation)
        listed = client.get("/management/v1/erasures").json()

    answered = [s["detail"] for s in verified["report"]["surfaces"] if s["surface"] == surface]
    assert (answered, _INTERNALS in str(listed)) == ([detail], False), (verified["report"]["surfaces"], listed)
