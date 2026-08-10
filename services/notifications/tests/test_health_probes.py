"""The three health surfaces, and the difference between reporting a fault and acting on one.

This service has a fault mode no other fleet member has: `register_actor` can fail and the process
survives it deliberately, because the read plane, the gateway row and the probes are all still worth
having while the actor plane is gone. That split only works if each surface does exactly one job:

* `/api/health` — the frontend badge. Touches nothing.
* `/livez` — "this event loop answers". Touches nothing, including the lifecycle flags. A liveness
  probe that consults a dependency turns a blip in that dependency into a restart loop, which is how a
  transient outage becomes an outage that cannot end.
* `/readyz` — reports the actor plane AS A COMPONENT of a 200. `service_kit.probes` renders
  `degraded` as a 503, so reporting it that way would pull the pod out of rotation — taking the health
  surface and the gateway row down with it and leaving an operator nothing to read. The refusal is the
  INBOX ROUTE's job, and that split is asserted here in one state rather than assumed.

The alternative is reproduced below and shown to 503, so "we report it as a component" is a choice
with a demonstrated consequence rather than a preference.
"""

from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from notifications import health, proxies
from notifications.api import inbox as inbox_module
from notifications.api import security as security_module
from notifications.config import get_notifications_settings
from notifications.lifespan import actor_plane_ready
from notifications.proxies import TypedActorProxy
from service_kit.exceptions import register_handlers
from service_kit.probes import make_probes_router
from service_kit.schemas.health import Readiness, ReadinessStatus


class _ExplodingInbox:
    """Every actor call raises. Nothing a probe does may reach this."""

    def __getattr__(self, name: str) -> Any:
        async def _boom(*_: object, **__: object) -> dict[str, Any]:
            raise AssertionError(f"a probe reached the actor plane via {name!r}")

        return _boom


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app composed the way `notifications/__init__.py` composes it: the badge under the api
    prefix, the operational pair ROOT-mounted (a kubelet does not know this service's prefix)."""
    app = FastAPI()
    register_handlers(app)
    app.include_router(health.router, prefix="/api")
    root = APIRouter()
    root.include_router(make_probes_router(actor_plane_ready))
    app.include_router(root)
    app.include_router(inbox_module.router, prefix="/api")
    app.state.notifications_settings = get_notifications_settings()
    app.state.fga = None
    app.dependency_overrides[security_module._deps.current_subject] = lambda: "alice"
    # Both seams to the actor plane explode: whatever a probe touches, it must not be this.
    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, _ExplodingInbox()))
    monkeypatch.setattr(proxies, "typed_proxy", _reject_proxy)
    with TestClient(app) as test_client:
        yield test_client


def _reject_proxy(*_: object, **__: object) -> TypedActorProxy:
    raise AssertionError("a probe opened a sidecar channel")


def test_the_frontend_badge_answers_with_nothing_wired(client: TestClient) -> None:
    """`/api/health` is what the chart's default `healthPath` points at. It reports the PROCESS, so it
    must answer identically whether or not the state store, the sidecar or OpenFGA exist — a badge that
    fails on a dependency blip restarts the pod for a reason that had already passed."""
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_liveness_answers_with_the_state_store_unreachable(client: TestClient) -> None:
    """The actor plane is unregistered, every actor call raises and opening a channel raises. Liveness
    is still 200, because it asks nothing."""
    client.app.state.actors_registered = False
    client.app.state.startup_complete = True

    assert client.get("/livez").json() == {"status": "ok"}


def test_liveness_does_not_consult_the_lifecycle_flags_either(client: TestClient) -> None:
    """Neither `startup_complete` nor `shutting_down` gates liveness. During a drain the pod is
    deliberately not READY and is emphatically still alive; a liveness that followed readiness would
    have the kubelet kill it mid-drain instead of letting it finish."""
    client.app.state.startup_complete = False
    client.app.state.shutting_down = True

    assert client.get("/livez").status_code == 200


def test_readiness_is_a_lifecycle_gate_before_it_is_a_component_report(client: TestClient) -> None:
    """Nothing is READY before the lifespan has run — including a process whose actors happen to be
    registered already, which is what makes this a gate rather than a summary."""
    client.app.state.startup_complete = False
    client.app.state.actors_registered = True

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == ReadinessStatus.starting


def test_a_draining_pod_reports_shutting_down_before_anything_else(client: TestClient) -> None:
    """Drain wins over every other answer: once the lifespan flips the flag the pod must leave
    rotation, and the check must not touch a dependency that is already closing."""
    client.app.state.startup_complete = True
    client.app.state.actors_registered = True
    client.app.state.shutting_down = True

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == ReadinessStatus.shutting_down


def test_an_unregistered_actor_plane_is_a_flag_on_a_200_and_not_a_503(client: TestClient) -> None:
    """The load-bearing one. A service whose inbox is dead is still a service an operator can reach:
    its health surface answers, its gateway row answers, and `/readyz` NAMES the broken component
    instead of hiding it behind a pod that has left rotation."""
    client.app.state.startup_complete = True
    client.app.state.actors_registered = False

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "components": {"actors": "unregistered"}}


def test_a_registered_actor_plane_reports_itself_by_name(client: TestClient) -> None:
    """The component is reported in BOTH states — a key that appears only when something is wrong is a
    key an operator cannot tell from a key that was never implemented."""
    client.app.state.startup_complete = True
    client.app.state.actors_registered = True

    assert client.get("/readyz").json() == {"status": "ready", "components": {"actors": "registered"}}


def test_a_missing_flag_reads_as_unregistered_rather_than_as_healthy(client: TestClient) -> None:
    """`notifications/__init__.py` sets the flag False at import so it is never merely absent — but the
    probe defaults it anyway, because the one state that must not read as healthy is the one where the
    flag was never written at all: a mount that failed before registration was ever attempted. This
    fixture writes no flag, which is exactly that state."""
    client.app.state.startup_complete = True

    assert not hasattr(client.app.state, "actors_registered")
    assert client.get("/readyz").json()["components"] == {"actors": "unregistered"}


@pytest.mark.asyncio
async def test_the_probe_never_reports_degraded_because_degraded_is_a_503() -> None:
    """The alternative, reproduced: a `ready_check` that answered `degraded` would 503.

    This is why `actor_plane_ready` returns `ready` with a component rather than `degraded` — the
    status is not cosmetic, `service_kit.probes` maps it straight onto the HTTP code that removes the
    pod from every Service that selects it.
    """
    app = FastAPI()
    app.include_router(make_probes_router(_degraded))
    app.state.startup_complete = True
    app.state.shutting_down = False
    app.state.actors_registered = False

    with TestClient(app) as client:
        pulled = client.get("/readyz")

    assert pulled.status_code == 503, "if degraded ever stops being a 503, re-derive what this probe reports"
    assert (await actor_plane_ready(cast(Request, _RequestOnto(app)))).status is ReadinessStatus.ready


async def _degraded(_: Request) -> Readiness:
    return Readiness(status=ReadinessStatus.degraded, components={"actors": "unregistered"})


class _RequestOnto:
    """The one attribute `actor_plane_ready` reads. A real `Request` needs a live ASGI scope, and the
    probe is a function over `app.state` — constructing the scope would test starlette."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app


def test_the_probe_reports_while_the_inbox_route_refuses(client: TestClient) -> None:
    """One state, two answers, and they are both correct.

    `/readyz` reporting a 200 is only safe because something else refuses the work: a route that
    answered an empty page here would tell a reader their inbox is empty when it is unreachable. The
    two halves are asserted together because either alone reads as a bug.
    """
    client.app.state.startup_complete = True
    client.app.state.actors_registered = False

    assert client.get("/readyz").status_code == 200
    refusal = client.get("/api/notifications/inbox")
    assert refusal.status_code == 503
    assert "actor plane" in refusal.json()["detail"]
