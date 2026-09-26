"""A GC run for a branch must reclaim the BRANCH's versions, and leave the parent's bytes alone.

[[LH-019]]. `maintenance/run` declared `branch` only to refuse it, and its reason was not about
branches at all: "this door's `sibling_base_refs` lists one parent directory and cannot see a referrer
under another root". Measured, that sentence is equally true of the MAIN request this door already
serves — a branch handle reports the DATASET ROOT as its `uri` (`ds.uri == branch.uri`, measured on
pylance 11.0.0), so the pre-pass lists the same directory and `is_protected` checks the same location
on both paths. A refusal that buys a guarantee the accepted path does not have buys nothing.

WHAT THE REFUSAL FEARED CANNOT HAPPEN, and this suite is where that is pinned rather than asserted.
Lance's cleanup is REF-SCOPED: measured on a branch that overwrote every parent fragment — the sharpest
case, where every one of the parent's files is garbage from the branch's point of view —
`cleanup_old_versions` removed one data file and it was the branch's own under `tree/<branch>/data`,
while the parent's `data/` stayed byte-identical and still time-travelled to v1. A branch keeps its own
`_versions/`/`_transactions/`/`data/` under `tree/{branch}/` (`lance_docs/file_format.md:2746-2761`),
and that layout is what contains the reclaim.

THE ASSERTION IS ON WHAT WAS DESTROYED, not on the call, and the last leg is the one that matters: a
door that opened the branch and then reclaimed main would satisfy a spy on `open_dataset` and still be
the irreversible defect. So the parent's data files are counted before and after.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespace

from catalog.api.dependencies import get_namespace, get_storage_options
from catalog.api.v1.endpoints import maintenance as door
from catalog.core.config import Settings, get_settings
from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

TABLE_ID = ["rows"]
BRANCH = "work"


def _rows(n: int, start: int = 0) -> pa.Table:
    return pa.table({"id": pa.array(range(start, start + n), pa.int64())})


@pytest.fixture
def namespace(tmp_path: Path) -> LanceNamespace:
    """Main OVERWRITTEN before the branch is cut, so the parent's first data files are garbage ON MAIN.

    That detail is what makes the survival leg a gate rather than decoration. With an append-only main
    no parent data file is ever reclaimable, so a door that reclaimed MAIN would leave them all in
    place and the leg would pass while the defect it hunts was present. Measured on this shape: a
    branch-scoped reclaim removes 0 parent data files, a main-scoped one removes 2.
    """
    ns = lance_namespace.connect("dir", {"root": str(tmp_path / "data")})
    create_table(ns, {}, TABLE_ID, _rows(1), mode="create")
    open_dataset(ns, {}, TABLE_ID).insert(_rows(1, start=1))
    lance.write_dataset(_rows(1, start=2), str(open_dataset(ns, {}, TABLE_ID).uri), mode="overwrite")
    open_dataset(ns, {}, TABLE_ID).create_branch(BRANCH, None)
    branch = open_dataset(ns, {}, TABLE_ID, branch=BRANCH)
    for i in range(3, 9):
        branch.insert(_rows(1, start=i))
    return ns


@pytest.fixture
def client(namespace: LanceNamespace) -> Iterator[TestClient]:
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: namespace
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application) as test_client:
        yield test_client


def _run(client: TestClient, *, branch: str | None) -> dict[str, Any]:
    suffix = f"?branch={branch}" if branch else ""
    response = client.post(f"/management/v1/table/rows/maintenance/run{suffix}", json={"retain_versions": 1})
    assert response.status_code == 200, response.text
    return response.json()


def _versions(namespace: LanceNamespace, *, branch: str | None) -> int:
    return len(open_dataset(namespace, {}, TABLE_ID, branch=branch).versions())


def _parent_data_files(namespace: LanceNamespace) -> list[str]:
    root = Path(str(open_dataset(namespace, {}, TABLE_ID).uri)) / "data"
    return sorted(entry.name for entry in root.iterdir())


def test_the_refs_really_diverged(namespace: LanceNamespace) -> None:
    """Without this the comparisons below could pass by reclaiming either ref."""
    main, branch = _versions(namespace, branch=None), _versions(namespace, branch=BRANCH)

    assert branch > main, f"the fixture did not diverge the refs (main={main}, branch={branch})"


def test_running_on_a_BRANCH_reclaims_the_branch(client: TestClient, namespace: LanceNamespace) -> None:
    """THE DEFECT: reclaiming main and reporting it as the branch's work destroys the wrong history."""
    before_main, before_branch = _versions(namespace, branch=None), _versions(namespace, branch=BRANCH)

    body = _run(client, branch=BRANCH)

    assert body["old_versions_removed"] > 0, f"nothing was reclaimed, so this proves nothing: {body}"
    assert _versions(namespace, branch=BRANCH) < before_branch, "the branch kept every version — the reclaim landed elsewhere"
    assert _versions(namespace, branch=None) == before_main, "MAIN lost versions to a branch-targeted reclaim"


def test_a_branch_reclaim_leaves_the_PARENTS_data_files_alone(client: TestClient, namespace: LanceNamespace) -> None:
    """The irreversible half. A branch resolves its inherited fragments through the parent's `data/`."""
    before = _parent_data_files(namespace)

    _run(client, branch=BRANCH)

    assert _parent_data_files(namespace) == before, f"a branch reclaim deleted parent data files: {set(before) - set(_parent_data_files(namespace))}"
    assert open_dataset(namespace, {}, TABLE_ID, version=1).count_rows() == 1, "main no longer time-travels to v1"


def test_running_WITHOUT_a_branch_still_reclaims_main(client: TestClient, namespace: LanceNamespace) -> None:
    """The control. A door stamping every request with a branch would pass above and fail here."""
    before_main, before_branch = _versions(namespace, branch=None), _versions(namespace, branch=BRANCH)

    body = _run(client, branch=None)

    assert body["old_versions_removed"] > 0, f"nothing was reclaimed on main: {body}"
    assert _versions(namespace, branch=None) < before_main, "main kept every version — the reclaim landed elsewhere"
    assert _versions(namespace, branch=BRANCH) == before_branch, "the BRANCH lost versions to a main-targeted reclaim"
