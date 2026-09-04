"""The lag cron's component name, env var and served path are ONE string, and it is opt-in.

Dapr delivers an input binding to `POST /<component-name>` at the pod ROOT — never under the API
prefix. So the Component's `metadata.name`, the app's setting, and the path FastAPI serves are the same
string three times, and any two of them agreeing while the third does not is a cron that fires into a
404 forever with every pod green. `rask-services-fleet` records this for the notifications reconciler,
where all three are rendered from one values key for exactly that reason.

OPT-IN, like the control relay and lineage's reconcile route: an unnamed binding means a deployment
with no component (or no sidecar), and mounting an always-live door for it adds a surface with nothing
behind it. The route is also `require_dapr_token`-guarded — unauthenticated, anything that can reach
the port could drive an unbounded catalog+lineage scan on demand.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from medallion.api.cascade_lag_cron import mount_lag_cron
from service_kit.lakehouse.ns_errors import install_problem_handlers


def _client(binding: str) -> tuple[TestClient, bool]:
    app = FastAPI()
    # The refusal raises `PermissionDeniedError`, which only becomes a 403 once the estate's problem
    # handlers are installed — the real app installs them, so a bare FastAPI here would report the
    # guard as a 500 and read as "unguarded".
    install_problem_handlers(app, logging.getLogger(__name__))
    mounted = mount_lag_cron(app, binding)
    return TestClient(app, raise_server_exceptions=False), mounted


def test_an_unnamed_binding_mounts_nothing() -> None:
    client, mounted = _client("")
    assert mounted is False
    assert client.post("/medallion-cascade-lag-cron").status_code == 404


def test_the_route_path_IS_the_binding_name() -> None:
    """Driven through the app rather than by reading `app.routes`: this FastAPI wraps an included
    router in `_IncludedRouter` and does not flatten it, so a structural probe reports "not mounted"
    for a route that serves perfectly. What Dapr cares about is whether the path answers."""
    client, mounted = _client("medallion-cascade-lag-cron")
    assert mounted is True
    assert client.post("/medallion-cascade-lag-cron").status_code != 404


def test_a_different_name_is_NOT_served() -> None:
    """The one-string rule from the other side: the path is the binding name and nothing else, so a
    Component whose name drifts from the setting hits a 404 rather than a silently working door."""
    client, _ = _client("medallion-cascade-lag-cron")
    assert client.post("/some-other-name").status_code == 404


def test_it_is_mounted_at_the_pod_ROOT_not_under_the_api_prefix() -> None:
    """Dapr posts to the pod root. A route under `/api` is a cron that 404s forever."""
    client, _ = _client("medallion-cascade-lag-cron")
    assert client.post("/api/medallion-cascade-lag-cron").status_code == 404
    assert client.post("/medallion-cascade-lag-cron").status_code != 404


def test_it_answers_OPTIONS_as_well_as_POST() -> None:
    """The sidecar probes with OPTIONS before delivering; a POST-only door is reported unroutable and
    the binding never delivers."""
    client, _ = _client("medallion-cascade-lag-cron")
    assert client.options("/medallion-cascade-lag-cron").status_code == 200


def test_the_door_REFUSES_a_front_door_invocation() -> None:
    """The guard's UNCONDITIONAL half, and the only one a test can assert without a configured token.

    `require_dapr_token`'s token check is a no-op when `APP_API_TOKEN` is unset — the open dev default,
    made a startup error by `assert_app_token_configured` once Dapr ingest is on. Asserting 401 here
    would therefore be asserting the dev default, not the guard. The public-caller refusal is
    deliberately NOT conditional on the token, because this route is sidecar-delivery-only by
    construction and a front-door invocation of it is never legitimate in any environment.
    """
    client, _ = _client("medallion-cascade-lag-cron")
    # `gateway` is the estate's declared public front door (`_DEFAULT_PUBLIC_CALLERS`). An ABSENT
    # header is deliberately NOT public — pub/sub, input bindings and Service-DNS calls all arrive
    # without one, and treating absence as public would break the cascade while closing nothing.
    response = client.post("/medallion-cascade-lag-cron", headers={"dapr-caller-app-id": "gateway"})
    assert response.status_code in {401, 403}, response.text
