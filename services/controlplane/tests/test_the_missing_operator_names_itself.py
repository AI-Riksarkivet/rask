"""A 404 from the k8s API is NOT an outage, and answering it as one has cost this estate twice.

`list_cluster_custom_object` 404s when the cluster registers no `projects.platform.rask.io`
resource type — i.e. when `rask-operator` (a SEPARATE repo; see `docs/DECISIONS.md`, *"Watch
enrolment does not wait for the `platform.rask.io` CRD"*, 2026-08-16) is not installed on this
estate. The API server ANSWERED; it said the type does not exist. That is a permanent property of
the deployment, not a reachability problem.

`routes.py` collapsed it — with an RBAC 403 and a genuine connection failure — into one
`503 cannot reach kubernetes api`. The two misdiagnoses that cost sessions:

  * `HANDOFF-lakehouse.md:101-106` recorded the 503 live and attributed it to
    **ServiceAccount/RBAC**. The RBAC is correct; the resource type does not exist.
  * `OPEN-WORK.md` §G1 concluded from the same 503 that the chart must **ship the CRD** — the one
    fix `docs/DECISIONS.md` rules out, because a CRD without its out-of-repo controller yields
    unreconciled CRs that render as projects stuck mid-provision.

So the contract these tests pin is discrimination, not tolerance. THE 404 IS NEVER SWALLOWED: an
empty `200 {"projects": []}` would be the worst answer of all — a gallery that looks successful on
an estate that has no project operator — and `test_a_missing_operator_is_never_an_empty_list`
refuses exactly that shape.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException


@pytest.fixture
def client() -> Iterator[TestClient]:
    from controlplane import app

    with TestClient(app) as c:
        yield c


def _raising_reader(exc: BaseException) -> object:
    class Reader:
        def list_projects(self) -> list[dict[str, Any]]:
            raise exc

        def ingress_hosts(self) -> dict[str, str]:
            return {}

    return Reader()


def _get(client: TestClient, exc: BaseException) -> Any:
    from controlplane import app
    from controlplane.routes import get_reader

    reader = _raising_reader(exc)
    app.dependency_overrides[get_reader] = lambda: reader
    try:
        return client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()


def test_a_missing_project_operator_says_so(client: TestClient) -> None:
    """The 404 answer names the resource type the cluster does not register."""
    resp = _get(client, ApiException(status=404, reason="Not Found"))

    detail = resp.json()["detail"]
    assert "projects.platform.rask.io" in detail
    assert "cannot reach kubernetes api" not in detail


def test_a_missing_project_operator_is_not_reported_as_unreachable(client: TestClient) -> None:
    """501, not 503: the capability is absent from this deployment, not temporarily down.

    A permanent 503 also tells every retrying client that the service is flapping, which it is not,
    and spends the estate's `invokeRetry` budget on an answer that cannot change — the retry policy
    matches on status, so 503 is retried and 501 is not.

    NOT because of circuit breakers: this estate deliberately runs none on invocation targets
    (`chart/templates/dapr-resiliency.yaml` — the previous one counted every non-2xx, so five
    authorization refusals took an app-id offline for 30s, a DoS any unauthenticated client could
    aim). An earlier version of this docstring cited them; it was wrong.
    """
    resp = _get(client, ApiException(status=404, reason="Not Found"))

    assert resp.status_code == 501


def test_a_missing_operator_is_never_an_empty_list(client: TestClient) -> None:
    """The forbidden shape. An estate with no operator must not render a successful empty gallery."""
    resp = _get(client, ApiException(status=404, reason="Not Found"))

    assert resp.status_code != 200
    assert "projects" not in resp.json()


def test_an_rbac_denial_is_not_blamed_on_a_missing_operator(client: TestClient) -> None:
    """403 = the type exists and this ServiceAccount may not read it. The opposite diagnosis."""
    resp = _get(client, ApiException(status=403, reason="Forbidden"))

    assert resp.status_code == 503
    assert "platform.rask.io" not in resp.json()["detail"]


def test_a_transport_failure_is_still_unreachable(client: TestClient) -> None:
    """The genuinely-transient case keeps its own answer, so the two stay distinguishable."""
    resp = _get(client, ConnectionError("k8s unreachable"))

    assert resp.status_code == 503
    assert resp.json()["detail"] == "cannot reach kubernetes api"
