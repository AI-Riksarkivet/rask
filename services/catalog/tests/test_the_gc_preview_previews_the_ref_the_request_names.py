"""A GC preview for a branch must describe the BRANCH's versions, not main's.

[[LH-019]]. `maintenance/preview` declared `branch` only to refuse it, under the module rule that a door
answering 200 for a branch it ignored has told the caller their branch was handled. That refusal was
right while the door opened main whatever the request said.

WHY THIS DOOR CAN OPEN NOW, when its two destructive siblings still cannot. The three were gated as one
on [[LH-094]]'s reclaim question, but they are not alike: measured here, `preview_maintenance` calls
`_base_refs` ZERO times and mutates nothing — `preview_gc` reads `ds.version`, `ds.versions()` and the
tags, and returns. It cannot reclaim anything on any ref, so the question that gates `/run` and
`/compact` does not reach it.

THE ASSERTION IS ON THE ANSWER, not on the call. A door that passed the branch to `open_dataset` and
then previewed main would satisfy a spy and still be the defect; what a caller acts on is the version
list, so that is what is compared — and the fixture diverges the two refs first, or both answers would
look alike and the test would pass on either.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace, get_storage_options
from catalog.api.v1.endpoints import maintenance as door
from catalog.core.config import Settings, get_settings
from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

TABLE_ID = ["rows"]
BRANCH = "work"


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _rows(n: int, start: int = 0) -> pa.Table:
    return pa.table({"id": pa.array(range(start, start + n), pa.int64())})


@pytest.fixture
def namespace(tmp_path: Path):  # noqa: ANN201 — LanceNamespace is runtime-only
    """Main at two versions, the branch at several — so a preview can tell the refs apart."""
    ns = __import__("lance_namespace").connect("dir", {"root": str(tmp_path / "data")})
    create_table(ns, {}, TABLE_ID, _ipc(_rows(1)), mode="create")
    open_dataset(ns, {}, TABLE_ID).insert(_rows(1, start=1))
    open_dataset(ns, {}, TABLE_ID).create_branch(BRANCH, None)
    branch = open_dataset(ns, {}, TABLE_ID, branch=BRANCH)
    for i in range(2, 7):
        branch.insert(_rows(1, start=i))
    return ns


@pytest.fixture
def client(namespace: Any) -> Iterator[TestClient]:  # noqa: ANN401 — the runtime namespace handle
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: namespace
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application) as test_client:
        yield test_client


def _preview(client: TestClient, *, branch: str | None) -> dict[str, Any]:
    suffix = f"?branch={branch}" if branch else ""
    response = client.post(f"/v1/table/rows/maintenance/preview{suffix}", json={"retain_versions": 1})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_refs_really_diverged(namespace: Any) -> None:  # noqa: ANN401
    """Without this the comparison below could pass by previewing either ref."""
    main_versions = len(open_dataset(namespace, {}, TABLE_ID).versions())
    branch_versions = len(open_dataset(namespace, {}, TABLE_ID, branch=BRANCH).versions())

    assert branch_versions > main_versions, f"the fixture did not diverge the refs (main={main_versions}, branch={branch_versions})"


def test_previewing_a_BRANCH_describes_the_branch(client: TestClient, namespace: Any) -> None:  # noqa: ANN401
    """THE DEFECT: previewing main and labelling it the branch's is a 200 that misinforms."""
    branch_current = open_dataset(namespace, {}, TABLE_ID, branch=BRANCH).version

    body = _preview(client, branch=BRANCH)

    assert body["current_version"] == branch_current, f"the preview describes version {body['current_version']} while the branch is at {branch_current}"


def test_previewing_WITHOUT_a_branch_still_describes_main(client: TestClient, namespace: Any) -> None:  # noqa: ANN401
    """The control. A door stamping every request with a branch would pass above and fail here."""
    main_current = open_dataset(namespace, {}, TABLE_ID).version

    body = _preview(client, branch=None)

    assert body["current_version"] == main_current


def test_the_two_refs_preview_DIFFERENT_work(client: TestClient) -> None:
    """The reclaim candidates are what a caller acts on, so the two refs must not answer alike."""
    on_branch = _preview(client, branch=BRANCH)
    on_main = _preview(client, branch=None)

    assert on_branch != on_main, "both refs previewed identically — the branch was ignored"
