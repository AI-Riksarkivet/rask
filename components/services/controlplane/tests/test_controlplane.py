"""controlplane tests — health skeleton + (later) project listing."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # RASK_API_PREFIX=/api mirrors the deployed fleet; the shared Settings also
    # *requires* viewer in/out, so set dummies even though controlplane ignores them.
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")

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

    dto = to_dto(_cr("demo", team="team-archives", phase="Ready"))
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

    dto = to_dto(_cr("fresh", phase=None))
    assert dto.phase == "Pending"
    assert dto.namespace == ""


def test_to_dto_empty_phase_defaults_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")

    from controlplane.service import to_dto

    cr = _cr("empty", phase="Ready")
    cr["status"]["phase"] = ""  # status present, phase empty string
    dto = to_dto(cr)
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

    dtos = list_project_dtos(FakeReader())
    assert [d.slug for d in dtos] == ["a", "b"]
