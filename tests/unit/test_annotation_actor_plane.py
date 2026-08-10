"""The task plane's 503 gate — `app.state.actors_registered`, finally read by something.

`annotator.main` registers three actor types in its lifespan and keeps a failure NON-FATAL on
purpose: the read-plane annotation routes need no actors, so a broken task plane must not take the
media surface down with it. It documented the consequence as "the task endpoints surface it as a 503
instead" — and nothing consulted the flag, so a failed registration answered every task route with
whatever the actor call happened to raise. (open_dapr.md, `main.py:94`.)

These pin both halves of the promise: the task routes refuse with a reason, and the pod stays READY
so the read plane keeps serving.

**And the third thing, which is the gate's SCOPE.** `ActorRuntime.register_actor` is local — it
never contacts daprd — so the flag says an actor CLASS registered and says nothing about whether a
sidecar exists. The last test here pins that against the SDK rather than against a comment, because
every docstring around this gate now depends on it.
"""

from __future__ import annotations

from typing import Any

import pytest
from annotator.api.security import current_subject, get_checker
from annotator.api.v1.endpoints import tasks as tasks_ep
from annotator.projects.models import ProjectState, TaskState
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from service_kit.exceptions import ServiceUnavailableError, register_handlers


SUBJECT = "gina"
PROJECT_ID = "proj-1"

#: The whole task surface, read routes included: every one of them reaches the task actor, so none
#: can answer while the plane is down.
TASK_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "/tasks/t1/events", {"json": {"event": "claim"}}),
    ("GET", "/tasks/t1", {}),
    ("PUT", "/tasks/t1/draft", {"json": {"shapes": []}}),
    ("POST", "/tasks/t1/import", {"content": b""}),
    ("GET", "/tasks/t1/draft", {}),
]


class _Unreachable:
    """The actor plane, when it is down. Any call is the failure this gate exists to prevent."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the task plane called {name}() while its actors were unregistered")


class _LiveTask:
    """A reachable task actor, for proving the gate is invisible when the plane is up."""

    async def get(self) -> dict[str, Any]:
        return {"state": TaskState.CLAIMED, "assignee": SUBJECT, "submitted_by": None, "task_id": "t1", "project_id": PROJECT_ID}

    async def get_draft(self) -> dict[str, Any]:
        return {"revision": 1, "shapes": [], "links": []}


def _app(actors_registered: bool | None) -> FastAPI:
    async def checker(*, user: str, relation: str, obj: str) -> bool:  # noqa: ARG001
        return True

    app = FastAPI()
    register_handlers(app)
    app.include_router(tasks_ep.router)
    app.dependency_overrides[get_checker] = lambda: checker
    app.dependency_overrides[current_subject] = lambda: SUBJECT
    # Absent is a THIRD state, not a missing second one — see the three-state test below.
    if actors_registered is not None:
        app.state.actors_registered = actors_registered
    return app


def _req(**state: Any) -> Request:
    """A REAL `Request` over a real app, carrying the one thing `require_actor_plane` reads off it.

    Built rather than faked, so the type the gate declares is the type under test: `request.app`
    resolves through starlette's own scope, exactly as in prod.
    """
    app = FastAPI()
    for key, value in state.items():
        setattr(app.state, key, value)
    scope: dict[str, Any] = {"type": "http", "app": app}
    return Request(scope)


def test_the_gate_refuses_only_an_EXPLICITLY_broken_actor_plane() -> None:
    """Three states, and only the middle one refuses: registered, mounted-and-broken, and ABSENT.

    Absent means a composition that makes no claim about an actor plane at all. Refusing there would
    invent an outage instead of reporting one, and would put this router's behaviour at the mercy of
    whether some other module remembered to set a flag.
    """
    tasks_ep.require_actor_plane(_req(actors_registered=True))
    tasks_ep.require_actor_plane(_req())
    with pytest.raises(ServiceUnavailableError):
        tasks_ep.require_actor_plane(_req(actors_registered=False))


@pytest.mark.parametrize(("method", "path", "kwargs"), TASK_ROUTES)
def test_every_task_route_503s_when_its_actors_are_not_registered(method: str, path: str, kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """And short-circuits BEFORE the actor — the point is to stop producing opaque sidecar errors,
    not to produce them behind a nicer status code."""
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: _Unreachable())
    client = TestClient(_app(actors_registered=False))

    r = client.request(method, path, **kwargs)

    assert r.status_code == 503, r.text
    assert "not registered" in r.text, "the 503 must say WHY, or it is the opaque error it replaces"


@pytest.mark.parametrize("actors_registered", [True, None])
def test_a_healthy_or_undeclared_actor_plane_is_not_gated(actors_registered: bool | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: _LiveTask())
    client = TestClient(_app(actors_registered))

    r = client.get("/tasks/t1")

    assert r.status_code == 200, r.text


def test_only_the_task_plane_carries_the_gate() -> None:
    """The read plane must stay up while the task plane is down — that is the whole reason
    registration is non-fatal. Asserted on the composed router rather than by driving a media route,
    so a future router that picks the dependency up by accident is caught here.

    **Walked with `iter_route_contexts`, not over `app.routes`.** FastAPI 0.140 made `include_router`
    LAZY: the app stores one opaque `_IncludedRouter` per include and never flattens it — not on
    startup, not on `openapi()` — so a walk over `app.routes` finds zero `APIRoute`s and this test
    failed as "the gate is mounted on nothing" while the gate was mounted correctly. The context form
    is also STRICTER than the private-attribute walk (`route.original_router.routes`) that the same
    upgrade provoked elsewhere: it reports the EFFECTIVE path and the EFFECTIVE dependency list, so a
    gate attached at an `include_router(..., dependencies=[…])` site — a leak the child router's own
    routes cannot show — is caught here too.
    """
    from annotator.api.v1.router import router as api_router

    app = FastAPI()
    app.include_router(api_router)
    gated: set[str] = set()
    for ctx in iter_route_contexts(app.routes):
        if not any(getattr(dep, "dependency", None) is tasks_ep.require_actor_plane for dep in getattr(ctx, "dependencies", ())):
            continue
        # Only some route kinds carry a `path`, and `RouteContext.path` is `str | None` for exactly
        # that reason. Asserted rather than filtered on: a gated route without one has to FAIL here,
        # not drop silently out of the set the assertions below read.
        path = ctx.path
        assert isinstance(ctx.original_route, APIRoute), f"a gated route is not an APIRoute: {ctx.original_route!r}"
        assert path is not None, f"a gated route carries no path: {ctx.original_route!r}"
        gated.add(path)

    assert gated, "the gate is mounted on nothing"
    assert all(path.startswith("/tasks") for path in gated), f"the actor-plane gate leaked onto non-task routes: {sorted(gated)}"


@pytest.mark.asyncio
async def test_readyz_reports_the_actor_plane_as_a_component_of_a_200() -> None:
    """`degraded` renders as a 503 in `service_kit.probes`, which would pull the pod from rotation and
    take the read plane down with the task plane — the exact coupling the non-fatal registration
    avoids. The probe REPORTS; the task routes are what refuse."""
    from annotator.main import _actor_plane_ready

    from service_kit.schemas.health import ReadinessStatus

    down = await _actor_plane_ready(_req(actors_registered=False))
    up = await _actor_plane_ready(_req(actors_registered=True))

    assert down.status is ReadinessStatus.ready, "an unregistered actor plane must not pull the read plane out of rotation"
    assert down.components["actors"] == "unregistered", "the outage has to be VISIBLE somewhere, or the operator is back to guessing"
    assert up.components["actors"] == "registered"


def test_the_flag_is_defined_before_the_first_request() -> None:
    """A flag that exists only on the happy path cannot be the thing a route gates on: a DaprActor
    mount that failed never reaches the registration block, so `main` defines it at import."""
    from annotator.main import app as annotator_app

    assert isinstance(getattr(annotator_app.state, "actors_registered", None), bool)


def test_the_gate_precedes_the_project_read_so_a_down_plane_costs_no_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 that still spends an actor read per request is a slower outage, not a handled one."""
    from annotator.api.v1.endpoints import project_events as pe

    reads: list[str] = []

    class _Project:
        async def get(self) -> dict[str, Any]:
            reads.append("project")
            return {"state": ProjectState.LABELING, "project_id": PROJECT_ID, "consensus_n": 1}

    monkeypatch.setattr(tasks_ep, "_proxy", lambda _t: _Unreachable())
    monkeypatch.setattr(pe, "_project_proxy", lambda _p: _Project())
    client = TestClient(_app(actors_registered=False))

    assert client.post("/tasks/t1/events", json={"event": "claim"}).status_code == 503
    assert reads == [], "the gate let the request reach the project actor before refusing"


@pytest.mark.asyncio
async def test_registering_an_actor_type_proves_nothing_about_a_SIDECAR() -> None:
    """The gate's honest scope, pinned against the SDK instead of asserted in a comment.

    `ActorRuntime.register_actor` builds the type info, constructs an actor client WITHOUT invoking
    it, and stores an `ActorManager` in a process dict; daprd learns the entity list afterwards by
    POLLING `/dapr/config`. So it succeeds here — in a unit-test process with no daprd anywhere —
    and `actors_registered` is therefore `True` in every no-sidecar composition. The 503 gate covers
    a malformed actor CLASS, NOT an absent or unreachable sidecar, and `main.py` /
    `tasks.require_actor_plane` say exactly that. If a future SDK makes registration remote this
    fails, and those docstrings stop being quietly wrong.
    """
    from annotator.projects.actor import AnnotationTaskActor
    from dapr.actor.runtime.runtime import ActorRuntime

    await ActorRuntime.register_actor(AnnotationTaskActor)

    assert "AnnotationTaskActor" in ActorRuntime.get_registered_actor_types()
