"""An erasure a branch or tag pins tells each person, then releases the pin at its deadline ([[LH-178]], ADR 0138).

The pinned shape is measured on pylance 12.0.0: a store that cannot rewrite ``work`` leaves the subject in
``work``'s files, and ``work``'s head stands on main v2's, so main v2 keeps answering after main's own
cleanup. ``pinned_by`` then names ``tag:t1`` (on ``deeper``), ``branch:deeper`` and ``branch:work``, the
order Lance accepts their deletion in. Lance refuses to delete a branch another branch is cut from, so the
release takes the subtree leaf-first and recreates it top-down: ``work`` from main's head, ``deeper`` from
``work`` (ADR 0138, ``a1b_child_branch_release.py`` on branch ``prototype/lance-erasure-and-commit-door``).

Driven through the erasure door and the release's cron door on one app, against real pylance on
``tmp_path``: the claim is the whole path a person sees, from the notice to an erasure that completes.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance.dataset import DatasetOptimizer
from lance_namespace import LanceNamespace

from catalog.api import erasure_release as release_door
from catalog.api.dependencies import get_clock, get_control_emitter, get_namespace, get_storage_options
from catalog.api.security import authenticate
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings, get_settings
from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table, read_arrow_body
from service_kit.control_events import CatalogControlEvent
from service_kit.lakehouse.ns_errors import install_problem_handlers


_PREDICATE = "pii = 'alice'"
_TABLE_ID = ["subjects"]
_BINDING = "catalog-erasure-release-cron"
_NOTICE = "erasure_release_scheduled"


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _pinned_table(root: Path) -> LanceNamespace:
    """main v1 alice+bob, v2 carol; ``work`` cut from main v2 gains eve at v3; ``deeper`` cut from (work, 3), tagged ``t1``."""
    ns = lance_namespace.connect("dir", {"root": str(root)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _rows("alice", "bob").schema) as writer:
        writer.write_table(_rows("alice", "bob"))
    create_table(ns, {}, _TABLE_ID, read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=1 << 20), mode="create", registry=None)
    open_dataset(ns, {}, _TABLE_ID).insert(_rows("carol"))
    lance.write_dataset(_rows("eve"), open_dataset(ns, {}, _TABLE_ID).create_branch("work", 2), mode="append")
    open_dataset(ns, {}, _TABLE_ID).create_branch("deeper", ("work", 3))
    open_dataset(ns, {}, _TABLE_ID).tags.create("t1", ("deeper", 3))
    return ns


def _a_store_that_cannot_rewrite_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every rewrite of a ref holding ``work``'s own row: the fault that leaves ``work`` pinning main v2."""
    compact = DatasetOptimizer.compact_files

    def _refuse_work(self: DatasetOptimizer, **options: Any) -> Any:
        if self._dataset.count_rows(filter="pii = 'eve'"):
            raise OSError("the store cannot rewrite this ref")
        return compact(self, **options)

    monkeypatch.setattr(DatasetOptimizer, "compact_files", _refuse_work)


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

    async def _owners(obj: str) -> list[str]:
        assert obj == "table:subjects"
        return ["user:olivia", "user:alice"]

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: ns
    application.dependency_overrides[get_storage_options] = lambda: {}
    application.dependency_overrides[authenticate] = lambda: SimpleNamespace(sub="alice")
    application.dependency_overrides[get_control_emitter] = lambda: emitter
    application.dependency_overrides[door.get_table_owners] = lambda: _owners
    application.dependency_overrides[get_clock] = lambda: clock
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def _branches(ns: LanceNamespace) -> dict[str, tuple[str | None, int]]:
    return {name: (meta["parent_branch"], meta["parent_version"]) for name, meta in open_dataset(ns, {}, _TABLE_ID).branches.list().items()}


def _erase(client: TestClient) -> dict[str, Any]:
    response = client.post("/management/v1/table/subjects/erasure", json={"predicate": _PREDICATE, "retain_days": 0})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_branch_pinned_erasure_tells_each_person_then_releases_the_subtree_at_its_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ns = _pinned_table(tmp_path / "data")
    _a_store_that_cannot_rewrite_work(monkeypatch)
    emitter, clock = _Recorder(), _Clock()

    with _catalog(ns, tmp_path, grace_days=7, emitter=emitter, clock=clock) as client:
        report = _erase(client)
        refs = ["tag:t1", "branch:deeper", "branch:work"]
        assert [pin["ref"] for pin in report["pinned_by"]] == refs
        release = report["release"]
        due = datetime.fromisoformat(release["due_at"])
        assert (release["refs"], release["notified"], due) == (refs, ["user:alice", "user:olivia"], clock.now + timedelta(days=7))
        notices = [event for event in emitter.events if event.action == _NOTICE]
        assert [(e.object_id, e.actor, e.extra["subject"], e.extra["refs"], datetime.fromisoformat(e.extra["due_at"])) for e in notices] == [
            ("table:subjects", "user:alice", subject, refs, due) for subject in ("user:alice", "user:olivia")
        ]

        clock.now = due - timedelta(seconds=1)
        assert client.post(f"/{_BINDING}").status_code == 200
        assert _branches(ns) == {"work": (None, 2), "deeper": ("work", 3)}, "nothing is released before the deadline"

        clock.now = due + timedelta(seconds=1)
        assert client.post(f"/{_BINDING}").status_code == 200
        head = open_dataset(ns, {}, _TABLE_ID).version
        again = _erase(client)

    assert (again["complete"], again["residual_versions"], again["release"]) == (True, [], None), again["surfaces"]
    assert _branches(ns) == {"work": (None, head), "deeper": ("work", head)}
    assert "t1" not in open_dataset(ns, {}, _TABLE_ID).tags.list()


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
    ns = _pinned_table(tmp_path / "data")
    _a_store_that_cannot_rewrite_work(monkeypatch)
    emitter, clock = _Recorder(), _Clock()

    with _catalog(ns, tmp_path, grace_days=0, emitter=emitter, clock=clock) as client:
        _erase(client)
        recorded = open_dataset(ns, {}, _TABLE_ID).version
        if dies_in == "recreate":
            _dies_once(monkeypatch, lance.LanceDataset, "create_branch", after=0)
        else:
            _dies_once(monkeypatch, type(open_dataset(ns, {}, _TABLE_ID).branches), "delete", after=1)
        client.post(f"/{_BINDING}")
        expected = {"work": (None, recorded), "deeper": ("work", recorded)}
        if dies_in == "deletes":
            assert set(_branches(ns)) == {"work"}, "the first tick deleted the leaf and died before its parent"
            open_dataset(ns, {}, _TABLE_ID).create_branch("late", ("work", None))
            expected["late"] = ("work", recorded)
        else:
            assert _branches(ns) == {}, "the first tick deleted the subtree and died before recreating it"
        open_dataset(ns, {}, _TABLE_ID).insert(_rows("zed"))
        assert client.post(f"/{_BINDING}").status_code == 200

    assert _branches(ns) == expected
