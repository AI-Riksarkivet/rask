"""A Project CR is untrusted input from another repo's operator — validate it, don't `.get()` it.

`service.py` walked the CR as `dict[str, Any]` with eight `.get(..., "")` calls and the double chain
`(spec.get("workload") or {}).get("type", "")` (CP-CR-UNVALIDATED). The CRD is published by
`rask-operator`, which lives in a SEPARATE repo and versions independently, so the shape of what the
API server hands back is exactly the kind of thing a boundary exists to check.

What the `.get()` chains did with a CR that did not match: they crashed inside the mapping, on a
line that names no CR. `{"spec": {"workload": "htr"}}` — a scalar where the CRD says object, one
schema revision away — raised `AttributeError: 'str' object has no attribute 'get'`, which
`routes._K8S_ERRORS` deliberately does not catch, so the caller got a bare 500 and the log got a
traceback pointing at `service.py` rather than at the CR that caused it. `{"metadata": null}` did the
same thing one line earlier.

The route's own rule is that a failure is NAMED — it 501s an absent operator rather than answering an
empty 200, precisely so nobody debugs a configuration fault as a data fault. An unreadable CR gets
the same treatment: a 502 that says the cluster returned a Project this service cannot read.

The three well-formed shapes are pinned here too, unchanged, because a boundary model that quietly
tightened them would break the gallery for CRs the old chain accepted.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    from controlplane import app

    with TestClient(app) as c:
        yield c


def _reader(crs: list[dict[str, Any]]):
    class _Reader:
        def list_projects(self) -> list[dict[str, Any]]:
            return crs

        def ingress_hosts(self) -> dict[str, str]:
            return {}

    return _Reader()


@pytest.mark.parametrize(
    ("label", "cr"),
    [
        ("a scalar where the CRD says object", {"metadata": {"name": "a"}, "spec": {"team": "t", "workload": "htr"}}),
        ("a null metadata block", {"metadata": None, "spec": {"team": "t", "workload": {"type": "htr"}}}),
        ("a non-string phase", {"metadata": {"name": "a"}, "spec": {"team": "t"}, "status": {"phase": 3}}),
    ],
)
def test_an_unreadable_cr_is_a_named_failure_not_an_attribute_error(client: TestClient, label: str, cr: dict[str, Any]) -> None:
    from controlplane import app
    from controlplane.routes import get_reader

    app.dependency_overrides[get_reader] = lambda: _reader([cr])
    try:
        response = client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 502, f"{label} must be a named refusal, not a crash inside the mapping"
    assert "Project" in response.json()["detail"]


def test_the_shapes_the_old_chain_accepted_still_map(client: TestClient) -> None:
    """No silent tightening: a missing status, an empty phase and an absent workload all still map."""
    from controlplane import app
    from controlplane.routes import get_reader

    crs = [
        {"metadata": {"name": "fresh", "creationTimestamp": "2026-01-01T00:00:00Z"}, "spec": {"team": "t"}},
        {
            "metadata": {"name": "empty", "creationTimestamp": "2026-01-02T00:00:00Z"},
            "spec": {"team": "t", "workload": {"type": "dummy"}},
            "status": {"phase": "", "namespace": "project-empty"},
        },
    ]
    app.dependency_overrides[get_reader] = lambda: _reader(crs)
    try:
        response = client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    projects = response.json()["projects"]
    assert [p["slug"] for p in projects] == ["fresh", "empty"]
    assert projects[0]["phase"] == "Pending"
    assert projects[0]["namespace"] == ""
    assert projects[0]["workload"] == ""
    assert projects[1]["phase"] == "Pending"
    assert projects[1]["namespace"] == "project-empty"
    assert projects[1]["workload"] == "dummy"


def test_the_cr_model_is_what_the_mapper_takes() -> None:
    """The mapper's input is a MODEL — the `.get()` chains have no home left to live in."""
    from controlplane.schemas import ProjectCR
    from controlplane.service import to_dto

    cr = ProjectCR.model_validate({"metadata": {"name": "demo"}, "spec": {"team": "t", "workload": {"type": "dummy"}}})
    assert to_dto(cr, "http://demo.test/overview").workload == "dummy"
