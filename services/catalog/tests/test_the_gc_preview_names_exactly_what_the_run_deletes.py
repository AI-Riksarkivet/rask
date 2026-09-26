"""The GC preview must name exactly the versions the GC run then deletes — on every ref.

The preview is the operator's pre-flight for an irreversible call, so the only correct answer is the
run's own: a version the preview offers and the run keeps misinforms, and a version the preview calls
protected and the run deletes is data lost behind a promise. Each case below therefore previews one
ref, runs the reclaim with the same bounds through the ``/run`` door, and compares the preview to the
versions that actually disappeared.

THE TWO PINS THE PREVIEW HAS TO READ, as Lance records and honours them (measured on pylance 12.0.0,
2026-09-25, against ``cleanup_old_versions`` itself):

* A TAG PINS A VERSION OF ITS OWN BRANCH. Tags are stored once at the dataset root
  (``lance_docs/file_format.md`` "Tag Storage"), so ``tags.list()`` answers every branch's tags from any
  handle, each carrying the ``branch`` it names (``null`` for main — ``("main", 2)`` is stored as
  ``null`` too). A tag on ``work`` v3 does not protect MAIN's v3, and a main tag on v4 does not protect
  the BRANCH's v4: cleanup on either ref deletes the same-numbered version the other ref's tag names.
* A BRANCH PINS THE VERSION IT FORKED FROM on its parent (``parentBranch``/``parentVersion`` in
  ``_refs/branches/<name>.json``; the guide's "Lance ensures that cleanup does not delete files still
  referenced by any branch"). Main keeps v2 under ``cleanup_old_versions(older_than=0)`` while ``work``
  stands on it, and ``work`` keeps the version a branch of ITS OWN was cut from.

THE FIXTURE PUTS A PIN ON EACH SIDE OF EACH RULE, so a preview that reads either one wrongly
disagrees with the run on both refs: ``pinned`` tags ``work`` v3, ``release`` tags main v4, ``work``
forks from main v2, and ``deeper`` forks from ``work`` v5.
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
NESTED = "deeper"


def _rows(start: int) -> pa.Table:
    return pa.table({"id": pa.array([start], pa.int64())})


@pytest.fixture
def namespace(tmp_path: Path) -> LanceNamespace:
    """Main at v1..v6 and ``work`` at v2..v6, with one tag and one child branch pinning each ref."""
    ns = lance_namespace.connect("dir", {"root": str(tmp_path / "data")})
    create_table(ns, {}, TABLE_ID, _rows(0), mode="create")
    open_dataset(ns, {}, TABLE_ID).insert(_rows(1))
    open_dataset(ns, {}, TABLE_ID).create_branch(BRANCH, 2)
    for i in range(2, 6):
        open_dataset(ns, {}, TABLE_ID).insert(_rows(i))
    for i in range(10, 14):
        open_dataset(ns, {}, TABLE_ID, branch=BRANCH).insert(_rows(i))
    root = open_dataset(ns, {}, TABLE_ID)
    root.tags.create("pinned", (BRANCH, 3))
    root.tags.create("release", (None, 4))
    root.create_branch(NESTED, (BRANCH, 5))
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


def _call(client: TestClient, verb: str, *, branch: str | None, retain_versions: int) -> dict[str, Any]:
    suffix = f"?branch={branch}" if branch else ""
    response = client.post(f"/management/v1/table/rows/maintenance/{verb}{suffix}", json={"retain_versions": retain_versions})
    assert response.status_code == 200, response.text
    return response.json()


def _versions(namespace: LanceNamespace, *, branch: str | None) -> set[int]:
    return {int(v["version"]) for v in open_dataset(namespace, {}, TABLE_ID, branch=branch).versions()}


def test_the_fixture_carries_every_pin_the_comparison_depends_on(namespace: LanceNamespace) -> None:
    """Without these the comparison below could agree by accident: a ref with no pins previews itself correctly."""
    handle = open_dataset(namespace, {}, TABLE_ID)

    assert {name: (tag["branch"], tag["version"]) for name, tag in handle.tags.list().items()} == {"pinned": (BRANCH, 3), "release": (None, 4)}
    assert {name: (meta["parent_branch"], meta["parent_version"]) for name, meta in handle.branches.list().items()} == {BRANCH: (None, 2), NESTED: (BRANCH, 5)}
    assert _versions(namespace, branch=None) == {1, 2, 3, 4, 5, 6}
    assert _versions(namespace, branch=BRANCH) == {2, 3, 4, 5, 6}


@pytest.mark.parametrize("retain_versions", [1, 2])
@pytest.mark.parametrize("branch", [None, "main", BRANCH], ids=["main", "main-by-name", "branch"])
def test_the_preview_offers_exactly_the_versions_the_run_deletes(
    client: TestClient, namespace: LanceNamespace, branch: str | None, retain_versions: int
) -> None:
    """THE DEFECT. A preview that honours every listed tag and no fork offers main's v2 (the fork
    ``work`` stands on) and withholds main's v3 (tagged only on ``work``), while the run keeps v2 and
    deletes v3.

    ``main-by-name`` is its own case because Lance records main as null, never as ``"main"``: a
    request spelling the default ref must meet the same pins as one that omits it."""
    before = _versions(namespace, branch=branch)

    preview = _call(client, "preview", branch=branch, retain_versions=retain_versions)
    run = _call(client, "run", branch=branch, retain_versions=retain_versions)

    deleted = before - _versions(namespace, branch=branch)
    assert deleted, "the run reclaimed nothing, so agreeing with it proves nothing"
    assert sorted(preview["eligible_versions"]) == sorted(deleted), f"preview offered {sorted(preview['eligible_versions'])}, the run deleted {sorted(deleted)}"
    assert run["old_versions_removed"] == len(deleted)


@pytest.mark.parametrize(("branch", "expected"), [(None, {"release": 4}), (BRANCH, {"pinned": 3})], ids=["main", "branch"])
def test_the_preview_names_only_the_tags_that_pin_THIS_ref(client: TestClient, branch: str | None, expected: dict[str, int]) -> None:
    """A tag on another ref protects nothing here, so naming it as protection is the same misinformation
    as withholding the version it does not pin."""
    preview = _call(client, "preview", branch=branch, retain_versions=1)

    assert preview["protected_tags"] == expected
