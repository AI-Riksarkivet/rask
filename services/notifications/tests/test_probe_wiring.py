"""Where the SHIPPED app actually serves each surface — probes at the root, the inbox under the prefix.

`test_health_probes.py` composes an app by hand and proves what each probe *reports*. It cannot see
where they are MOUNTED, because it mounts them itself: root-mount `make_probes_router`, prefix-mount
the badge, and the suite is green whatever `notifications/__init__.py` does. That is the gap this
closes — it asks the real module-level `app`, which is the object a kubelet, a sidecar and the gateway
each talk to.

Three mounts, three different clients, and each has a different notion of where things live:

* **the kubelet** knows a port and a literal path (`chart/templates/fleet.yaml` renders
  `healthPath | default "/api/health"`) and nothing about `RASK_API_PREFIX`. The operational pair is
  root-mounted for the same reason — a supervisor cannot be asked to learn a service's api prefix.
* **daprd** calls `/dapr/config`, `/dapr/subscribe` and `/actors/...` at the POD ROOT. Move them under
  the prefix and actor placement and the subscription both die without an error anywhere: the pod is
  Ready, the bell is empty, and `/readyz` still says the actors registered locally.
* **the gateway** forwards `/api/notifications` unrewritten, so the door — and only the door — has to
  track the api prefix.

The prefix is therefore driven to a non-default value in this suite: at `/api` every one of these
claims is satisfied by an app that hardcodes `/api`, which is exactly how a mount that ignores the
setting ships unnoticed.
"""

import importlib
import os
from collections.abc import Iterator
from types import ModuleType

import pytest
from fastapi.testclient import TestClient


#: What the chart configures (`chart/values.yaml` `RASK_API_PREFIX: "/api"`) and what every other
#: suite in this repo assumes when it imports the app.
SHIPPED_PREFIX = "/api"

#: A prefix nothing hardcodes, used to tell "mounted under the setting" from "spelled `/api`".
OTHER_PREFIX = "/api/v1"

#: The sidecar's callback surface. Dapr addresses the app by port and calls these at the root; the
#: actor route is templated because the method name is part of the path.
SIDECAR_ROUTES = ("/dapr/config", "/dapr/subscribe", "/actors/{actor_type_name}/{actor_id}/method/{method_name}")


def _rebuilt_under(prefix: str) -> ModuleType:
    """Rebuild the module-level app under `prefix` and return the module.

    A reload rather than a second `make_service_app` call: `app` is a module-level singleton built at
    import from the environment, so the only way to ask what it serves under another prefix is to
    import it under one. `notifications/__init__.py` is import-safe by design — it mounts routes,
    registers no actor and opens no channel — which is what makes this cheap enough to do per test.
    """
    os.environ["RASK_API_PREFIX"] = prefix
    return importlib.reload(importlib.import_module("notifications"))


def _served_under(prefix: str) -> set[str]:
    return set(_rebuilt_under(prefix).app.openapi()["paths"])


@pytest.fixture(autouse=True)
def _leave_the_app_as_it_shipped() -> Iterator[None]:
    """Rebuild at the shipped prefix afterwards, always.

    `notifications.app` is process-global and two other suites read it (the gateway's rewrite check
    and its sidecar-route check), so a test that left it built under `/api/v1` would break them from a
    distance — the class of cross-suite coupling a module-scope env write causes, arriving through a
    reload instead. Restored inside the fixture rather than by `monkeypatch`, because unwinding the env
    does not rebuild the app.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RASK_API_PREFIX", SHIPPED_PREFIX)
        mp.setenv("RASK_DAPR_ENABLED", "false")
        yield
        _rebuilt_under(SHIPPED_PREFIX)


@pytest.mark.parametrize("probe", ["/livez", "/readyz"])
def test_the_operational_probes_are_root_mounted_whatever_the_api_prefix_is(probe: str) -> None:
    """A kubelet is configured with a port and a path, and knows nothing about `RASK_API_PREFIX`.

    Under the shipped prefix a mount that ignored the setting would be indistinguishable from this one,
    so the assertion is made under BOTH: the pair stays at the root when the door moves.
    """
    assert probe in _served_under(SHIPPED_PREFIX)
    assert probe in _served_under(OTHER_PREFIX)


@pytest.mark.parametrize("route", SIDECAR_ROUTES)
def test_the_sidecar_callback_surface_is_root_mounted_whatever_the_api_prefix_is(route: str) -> None:
    """daprd calls the app by port at the root: `/dapr/config` for the entity list, `/dapr/subscribe`
    for the subscription registration, `/actors/...` for every invocation, reminder and timer.

    Nothing errors if these move. The sidecar finds no entities, places no actor and registers no
    subscription; the pod is Ready, `/readyz` reports the actors registered — which is a LOCAL fact,
    as `require_actor_plane` says — and every inbox call 503s while the bell stays permanently empty.
    """
    assert route in _served_under(SHIPPED_PREFIX)
    assert route in _served_under(OTHER_PREFIX)


def test_the_api_prefix_moves_the_door_and_the_badge_and_nothing_else() -> None:
    """The other half of the two claims above, stated positively so neither can pass vacuously.

    If the prefix moved nothing at all, every assertion in this suite would hold and the app would be
    serving `/api/...` on a deployment configured for `/api/v1` — the gateway row interpolates its
    prefix (`services/gateway/tests/test_notifications_route.py`), so the two would silently diverge
    and every inbox call would 404 at a service that is up.
    """
    moved = _served_under(OTHER_PREFIX)

    assert "/api/v1/notifications/inbox" in moved
    assert "/api/v1/health" in moved
    assert "/api/notifications/inbox" not in moved
    assert "/api/health" not in moved


def test_the_badge_the_kubelet_probes_is_the_badge_the_app_serves() -> None:
    """`/api/health` under the shipped prefix — the literal `chart/templates/fleet.yaml` renders into
    both probes for this Deployment (`healthPath` is unset for `services.notifications`, so it takes
    the `/api/health` default).

    The chart half of this agreement is asserted in `tests/unit/test_invariants.py`; this is the app
    half, and neither alone would have caught a prefix change: the probe path is a literal in a values
    file and the badge's path is derived from an env var, and nothing renders them together.
    """
    assert "/api/health" in _served_under(SHIPPED_PREFIX)


def test_liveness_answers_on_a_pod_whose_lifespan_has_not_run() -> None:
    """No lifespan, no settings on `app.state`, no actor plane, no state store — and `/livez` is 200.

    Driven WITHOUT entering the TestClient context on purpose: that is the startup window a kubelet
    probes in, and a liveness that read `app.state` would fail there and have the pod killed before it
    could ever finish starting. `/readyz` is the contrast that proves this is a real distinction rather
    than "both probes happen to answer" — it reports `starting`, which is what keeps the pod out of
    rotation until the lifespan says otherwise.
    """
    app = _rebuilt_under(SHIPPED_PREFIX).app
    client = TestClient(app)

    assert client.get("/livez").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 503
    assert client.get("/readyz").json()["status"] == "starting"


def test_the_probe_paths_are_kept_out_of_the_trace_stream() -> None:
    """A kubelet polling twice a second is otherwise the loudest span in the service and carries no
    information — every trace-based RED metric for this app would be dominated by its own probes.

    The lever is the instrumentation's own env var, read when `opentelemetry.instrumentation.fastapi`
    is first imported, which is why `notifications/__init__.py` sets it BEFORE `make_service_app`
    (that is where `setup_otel` runs). Asserted as a membership rather than an equality: it is a
    `setdefault`, so a deployment may widen it.
    """
    _rebuilt_under(SHIPPED_PREFIX)

    excluded = {entry.strip() for entry in os.environ["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"].split(",")}

    assert {"livez", "readyz", "health"} <= excluded
