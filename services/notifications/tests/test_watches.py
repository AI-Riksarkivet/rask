"""The watch door and its two registries — v2 targeting.

The rule this file exists for is one sentence: **membership GATES watching and never implies it.** A
badge that counts other people's work is the failure the whole plane was built to end, and
"everyone in the project is told about everything in it" is that failure wearing a justification.
"""

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import watches as watches_module
from notifications.api.watches import WATCH_RELATION
from notifications.config import get_notifications_settings
from notifications.watch_actor import WATCHERS_KEY, WatchIndexActor


class _StateManager:
    """The actor state manager's contract in memory."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def save_state(self) -> None:
        return None


def _index() -> tuple[WatchIndexActor, _StateManager]:
    actor = WatchIndexActor.__new__(WatchIndexActor)
    state = _StateManager()
    # Cast rather than ignore: the fake satisfies the manager's contract structurally, and the SDK's
    # generic handle is not something a unit test should have to construct. Same shape as
    # `test_inbox_actor.py`.
    actor._state_manager = cast(Any, state)
    return actor, state


class TestWatchIndexActor:
    @pytest.mark.asyncio
    async def test_an_unwatched_project_lists_nobody(self) -> None:
        actor, _ = _index()
        assert await actor.list_watchers() == {"subjects": [], "total": 0}

    @pytest.mark.asyncio
    async def test_watching_registers_the_subject(self) -> None:
        actor, state = _index()
        assert await actor.watch({"subject": "alice"}) == {"added": True, "total": 1}
        assert await actor.list_watchers() == {"subjects": ["alice"], "total": 1}
        assert WATCHERS_KEY in state.store

    @pytest.mark.asyncio
    async def test_watching_twice_is_idempotent_rather_than_a_duplicate(self) -> None:
        """A retried request must not double-list — the `tenant_actor` rule, and the reason a fan-out
        can trust the list it reads."""
        actor, _ = _index()
        await actor.watch({"subject": "alice"})
        assert await actor.watch({"subject": "alice"}) == {"added": False, "total": 1}
        assert (await actor.list_watchers())["subjects"] == ["alice"]

    @pytest.mark.asyncio
    async def test_unwatching_removes_only_that_subject(self) -> None:
        actor, _ = _index()
        await actor.watch({"subject": "alice"})
        await actor.watch({"subject": "bob"})
        assert await actor.unwatch({"subject": "alice"}) == {"removed": True, "total": 1}
        assert (await actor.list_watchers())["subjects"] == ["bob"]

    @pytest.mark.asyncio
    async def test_unwatching_something_unwatched_is_not_an_error(self) -> None:
        actor, _ = _index()
        assert await actor.unwatch({"subject": "ghost"}) == {"removed": False, "total": 0}

    @pytest.mark.asyncio
    async def test_insertion_order_is_stable(self) -> None:
        """Free, and it keeps anything that renders the list from appearing to shuffle on its own."""
        actor, _ = _index()
        for subject in ("carol", "alice", "bob"):
            await actor.watch({"subject": subject})
        assert (await actor.list_watchers())["subjects"] == ["carol", "alice", "bob"]


class _FakeInbox:
    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def get_watches(self) -> dict[str, Any]:
        projects = self._plane.subject_watches.setdefault(self._subject, [])
        return {"projects": list(projects), "total": len(projects)}

    async def set_watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        projects = self._plane.subject_watches.setdefault(self._subject, [])
        project_id, watching = str(payload["project_id"]), bool(payload["watching"])
        changed = False
        if watching and project_id not in projects:
            projects.append(project_id)
            changed = True
        elif not watching and project_id in projects:
            projects.remove(project_id)
            changed = True
        return {"projects": list(projects), "total": len(projects), "changed": changed}


class _FakeIndex:
    def __init__(self, plane: "_Plane", project_id: str) -> None:
        self._plane = plane
        self._project = project_id

    async def watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        subjects = self._plane.project_watchers.setdefault(self._project, [])
        if payload["subject"] not in subjects:
            subjects.append(str(payload["subject"]))
        return {"added": True, "total": len(subjects)}

    async def unwatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        subjects = self._plane.project_watchers.setdefault(self._project, [])
        if payload["subject"] in subjects:
            subjects.remove(str(payload["subject"]))
        return {"removed": True, "total": len(subjects)}


class _Plane:
    def __init__(self) -> None:
        self.subject_watches: dict[str, list[str]] = {}
        self.project_watchers: dict[str, list[str]] = {}


def _app(plane: _Plane, *, allow: bool) -> FastAPI:
    """The watch router over a fake actor plane and a checker with a fixed verdict."""
    app = FastAPI()
    app.include_router(watches_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.fga = None
    app.state.actors_registered = True

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        assert relation == WATCH_RELATION
        assert obj.startswith("project:")
        return allow

    from notifications.api.security import _deps

    app.dependency_overrides[_deps.get_checker] = lambda: checker
    return app


@pytest.fixture
def plane(monkeypatch: pytest.MonkeyPatch) -> _Plane:
    p = _Plane()
    monkeypatch.setattr(watches_module, "inbox_for", lambda subject: _FakeInbox(p, subject))
    monkeypatch.setattr(watches_module, "watch_index_for", lambda project_id: _FakeIndex(p, project_id))
    return p


class TestWatchDoor:
    def test_a_member_may_watch_and_both_registries_are_written(self, plane: _Plane) -> None:
        """Neither registry can answer the other's question, so a watch that wrote one is half a watch."""
        client = TestClient(_app(plane, allow=True))

        body = client.put("/notifications/watches/acme").json()

        assert body == {"project_id": "acme", "watching": True, "changed": True, "total": 1}
        assert plane.project_watchers["acme"] == ["anon"], "the fan-out's view was not written"
        assert plane.subject_watches["anon"] == ["acme"], "the subject's own view was not written"

    def test_a_non_member_is_refused_and_nothing_is_written(self, plane: _Plane) -> None:
        """403 with a reason, never an empty 200 — and no residue in either registry."""
        client = TestClient(_app(plane, allow=False), raise_server_exceptions=False)

        response = client.put("/notifications/watches/acme")

        assert response.status_code == 403
        assert plane.project_watchers == {}
        assert plane.subject_watches == {}

    def test_watching_twice_reports_unchanged_rather_than_failing(self, plane: _Plane) -> None:
        client = TestClient(_app(plane, allow=True))
        client.put("/notifications/watches/acme")

        assert client.put("/notifications/watches/acme").json()["changed"] is False

    def test_the_list_reflects_what_was_watched(self, plane: _Plane) -> None:
        client = TestClient(_app(plane, allow=True))
        client.put("/notifications/watches/acme")
        client.put("/notifications/watches/beta")

        assert client.get("/notifications/watches").json() == {"projects": ["acme", "beta"], "total": 2}

    def test_unwatching_needs_no_membership(self, plane: _Plane) -> None:
        """The asymmetry is deliberate: someone removed from a project must still be able to clear a
        watch they can no longer create, or the preference becomes unreachable."""
        client = TestClient(_app(plane, allow=True))
        client.put("/notifications/watches/acme")

        denied = TestClient(_app(plane, allow=False))
        body = denied.delete("/notifications/watches/acme").json()

        assert body["watching"] is False
        assert plane.project_watchers["acme"] == []
        assert plane.subject_watches["anon"] == []

    def test_unwatching_something_unwatched_is_not_an_error(self, plane: _Plane) -> None:
        client = TestClient(_app(plane, allow=True))

        assert client.delete("/notifications/watches/ghost").json()["changed"] is False
