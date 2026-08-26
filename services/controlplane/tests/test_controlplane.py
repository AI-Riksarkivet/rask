"""controlplane tests — health endpoint + project listing (CR→DTO + reader seam)."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    # The env moved to conftest.py, and the move was a FIX rather than tidying. This fixture used to
    # `monkeypatch.setenv("RASK_API_PREFIX", "/api")` and import `controlplane` on the next line,
    # which is correct only while this fixture is the first thing to touch the package — the app is
    # built at module level, so the prefix is decided by the first importer. A second test module
    # importing `controlplane.routes` at collection time built it under the code default `/api/v1`
    # and every route here answered 404. conftest.py runs before test modules; monkeypatch cannot,
    # being function-scoped.
    from controlplane import app

    with TestClient(app) as c:
        yield c


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def _cr(name: str, *, team: str = "t", phase: str | None = "Ready", created: str = "2026-01-01T00:00:00Z") -> dict:
    cr: dict = {
        "metadata": {"name": name, "creationTimestamp": created},
        "spec": {"team": team, "workload": {"type": "htr"}},
    }
    if phase is not None:
        cr["status"] = {"phase": phase, "namespace": f"project-{name}"}
    return cr


def test_to_dto_maps_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")

    from controlplane.service import to_dto

    dto = to_dto(_cr("demo", team="team-archives", phase="Ready"), "")
    assert dto.slug == "demo"
    assert dto.name == "demo"
    assert dto.team == "team-archives"
    assert dto.workload == "htr"
    assert dto.phase == "Ready"
    assert dto.namespace == "project-demo"
    assert dto.created_at == "2026-01-01T00:00:00Z"


def test_to_dto_missing_status_defaults_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")

    from controlplane.service import to_dto

    dto = to_dto(_cr("fresh", phase=None), "")
    assert dto.phase == "Pending"
    assert dto.namespace == ""


def test_to_dto_empty_phase_defaults_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")

    from controlplane.service import to_dto

    cr = _cr("empty", phase="Ready")
    cr["status"]["phase"] = ""  # status present, phase empty string
    dto = to_dto(cr, "")
    assert dto.phase == "Pending"
    assert dto.namespace == "project-empty"  # namespace still preserved


def test_list_project_dtos_sorted_by_created_at(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")

    from controlplane.service import list_project_dtos

    class FakeReader:
        def list_projects(self) -> list[dict[str, Any]]:
            return [
                _cr("b", created="2026-02-01T00:00:00Z"),
                _cr("a", created="2026-01-01T00:00:00Z"),
            ]

        def ingress_host(self, namespace: str) -> str | None:
            return None

    dtos = list_project_dtos(FakeReader(), "http")
    assert [d.slug for d in dtos] == ["a", "b"]


def test_list_projects_endpoint_returns_dtos(client: TestClient) -> None:
    from controlplane import app
    from controlplane.routes import get_reader

    class FakeReader:
        def list_projects(self) -> list[dict]:
            return [_cr("demo", team="team-archives", phase="Ready")]

        def ingress_host(self, namespace: str) -> str | None:
            return None

    app.dependency_overrides[get_reader] = lambda: FakeReader()
    try:
        resp = client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["projects"][0]["slug"] == "demo"
    assert body["projects"][0]["phase"] == "Ready"
    assert body["projects"][0]["created_at"] == "2026-01-01T00:00:00Z"
    assert body["projects"][0]["url"] == ""


def test_list_projects_endpoint_503_on_k8s_unreachable(client: TestClient) -> None:
    """A real transport failure (connection refused → OSError subclass) → 503."""
    from controlplane import app
    from controlplane.routes import get_reader

    class UnreachableReader:
        def list_projects(self) -> list[dict]:
            raise ConnectionError("k8s unreachable")

        def ingress_host(self, namespace: str) -> str | None:
            return None

    app.dependency_overrides[get_reader] = lambda: UnreachableReader()
    try:
        resp = client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503


def test_list_projects_endpoint_does_not_mask_mapping_bug(client: TestClient) -> None:
    """A non-k8s error (e.g. a CR-mapping bug) must NOT be swallowed into a 503.
    It propagates as a server error (TestClient re-raises) so the defect is
    visible, instead of the old broad-except masking it as 'k8s unreachable'."""
    from controlplane import app
    from controlplane.routes import get_reader

    class BuggyReader:
        def list_projects(self) -> list[dict]:
            raise KeyError("unexpected CR shape")  # a programming bug, not k8s

        def ingress_host(self, namespace: str) -> str | None:
            return None

    app.dependency_overrides[get_reader] = lambda: BuggyReader()
    try:
        with pytest.raises(KeyError):
            client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()


def test_to_dto_builds_url_from_ingress_host() -> None:
    from controlplane.service import list_project_dtos

    class FakeReader:
        def list_projects(self) -> list[dict[str, Any]]:
            return [_cr("demo", phase="Ready")]

        def ingress_host(self, namespace: str) -> str | None:
            assert namespace == "project-demo"
            return "demo.rask.local"

    dtos = list_project_dtos(FakeReader(), "http")
    assert dtos[0].url == "http://demo.rask.local/overview"


def test_url_empty_when_no_ingress() -> None:
    from controlplane.service import list_project_dtos

    class FakeReader:
        def list_projects(self) -> list[dict[str, Any]]:
            return [_cr("demo", phase="Provisioning")]

        def ingress_host(self, namespace: str) -> str | None:
            return None

    dtos = list_project_dtos(FakeReader(), "http")
    assert dtos[0].url == ""
