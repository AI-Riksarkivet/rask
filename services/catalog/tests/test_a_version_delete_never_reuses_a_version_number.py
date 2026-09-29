"""A version delete removes only old, unpinned versions, so no version number is ever minted twice.

[[LH-206]]. Measured on pylance 12.0.0 against the `dir` backend's own `batch_delete_table_versions`
(ranges are start-inclusive, end-exclusive — the spec's `VersionRange`):

    {0, -1} on v1..v4          -> every manifest gone; describe 200, the table unopenable
    [4, 5) on v1..v4           -> latest back to 3; the next append minted 4 again
    [2, -1) on v1..v3, tag @2  -> latest back to 1; the next append minted 2 and the tag read its rows

`docs/DATA-CONTRACT.md` promises a pinned (dataset, version) stays bit-identical, and a feed consumer
checkpointed at a reused number skips the rows written under it.

Driven through the route against a real `dir` namespace.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespace, connect

from catalog.api.dependencies import NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.v1.endpoints import versions as version_door
from catalog.core.namespace import open_dataset
from catalog.services import maintenance
from catalog.services.dataplane import create_table
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

TABLE = ["alpha"]
BRANCH = "work"
#: v1 from the create, v2..v4 appended.
LATEST = 4


def _append(ns: LanceNamespace, value: int, *, branch: str | None = None) -> int:
    ds = open_dataset(ns, {}, TABLE, branch=branch)
    ds.insert(pa.table({"id": pa.array([value], pa.int64())}))
    return int(open_dataset(ns, {}, TABLE, branch=branch).version)


def _versions(ns: LanceNamespace, *, branch: str | None = None) -> list[int]:
    return [int(v["version"]) for v in open_dataset(ns, {}, TABLE, branch=branch).versions()]


@pytest.fixture
def ns(tmp_path: Path) -> LanceNamespace:
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE, pa.table({"id": pa.array([1], pa.int64())}), mode="create", registry=None)
    for value in range(2, LATEST + 1):
        _append(namespace, value)
    assert _versions(namespace) == [1, 2, 3, 4]
    return namespace


@pytest.fixture
def client(ns: LanceNamespace, tmp_path: Path) -> Iterator[TestClient]:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(version_door.router)
    settings = SimpleNamespace(delimiter="$", storage_options=dict, registry_root=str(tmp_path / "control"), external_blob_base_list=[])
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    with TestClient(application) as test_client:
        yield test_client


def _delete(client: TestClient, *ranges: tuple[int, int], branch: str | None = None) -> Any:  # noqa: ANN401 — an httpx Response
    body: dict[str, Any] = {"ranges": [{"start_version": start, "end_version": end} for start, end in ranges]}
    if branch is not None:
        body["branch"] = branch
    return client.post("/v1/table/alpha/version/delete", json=body)


def test_an_old_untagged_version_is_deleted(client: TestClient, ns: LanceNamespace) -> None:
    """The control: without it every refusal below would also pass against a door that deletes nothing."""
    answer = _delete(client, (1, 2))

    assert answer.status_code == 200, answer.text
    assert answer.json()["deleted_count"] == 1
    assert _versions(ns) == [2, 3, 4]


def test_the_spec_ALL_range_is_refused_and_the_table_still_opens(client: TestClient, ns: LanceNamespace) -> None:
    answer = _delete(client, (0, -1))

    assert answer.status_code == 400, answer.text
    assert answer.json()["code"] == 13
    assert _versions(ns) == [1, 2, 3, 4], "the refused range removed versions anyway"


@pytest.mark.parametrize("reaching", [(4, 5), (2, -1), (3, 9)], ids=["exactly-latest", "through-latest", "past-latest"])
def test_a_range_that_reaches_the_current_version_is_refused(client: TestClient, ns: LanceNamespace, reaching: tuple[int, int]) -> None:
    answer = _delete(client, reaching)

    assert answer.status_code == 400, answer.text
    assert answer.json()["code"] == 13
    assert "current version" in answer.json()["detail"]
    assert _versions(ns) == [1, 2, 3, 4]


def test_the_next_append_never_reuses_a_version_number(client: TestClient, ns: LanceNamespace) -> None:
    _delete(client, (LATEST, LATEST + 1))

    assert _append(ns, 99) == LATEST + 1, "a version number was minted twice"


def test_a_tagged_version_is_refused_and_the_tag_is_named(client: TestClient, ns: LanceNamespace) -> None:
    open_dataset(ns, {}, TABLE).tags.create("published", 2)

    answer = _delete(client, (1, 3))

    assert answer.status_code == 409, answer.text
    assert answer.json()["code"] == 19
    assert "published" in answer.json()["detail"]
    assert _versions(ns) == [1, 2, 3, 4], "a refusal that names a pin must delete nothing, the unpinned version 1 included"
    assert open_dataset(ns, {}, TABLE).checkout_version("published").to_table().column("id").to_pylist() == [1, 2]


def test_a_version_a_branch_was_cut_from_is_refused_and_the_branch_is_named(client: TestClient, ns: LanceNamespace) -> None:
    open_dataset(ns, {}, TABLE).create_branch("exp", 2)

    answer = _delete(client, (2, 3))

    assert answer.status_code == 409, answer.text
    assert answer.json()["code"] == 19
    assert "exp" in answer.json()["detail"]
    assert _versions(ns) == [1, 2, 3, 4]


@pytest.mark.parametrize("bad", [(3, 3), (3, 2), (-1, 2), (1, -2)], ids=["empty", "inverted", "negative-start", "negative-end"])
def test_a_range_that_names_no_version_is_refused(client: TestClient, ns: LanceNamespace, bad: tuple[int, int]) -> None:
    answer = _delete(client, bad)

    assert answer.status_code == 400, answer.text
    assert answer.json()["code"] == 13
    assert _versions(ns) == [1, 2, 3, 4]


def test_a_request_with_no_range_is_refused(client: TestClient, ns: LanceNamespace) -> None:
    """The spec requires `ranges` but sets no minimum, and pylance answers an empty list with an untyped
    OSError, which the catalog would report as its own 500."""
    answer = _delete(client)

    assert answer.status_code == 400, answer.text
    assert answer.json()["code"] == 13
    assert _versions(ns) == [1, 2, 3, 4]


def test_a_tag_that_lands_after_the_pin_check_is_still_refused_and_named(client: TestClient, ns: LanceNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-check reads the refs before the reclaim runs; a tag created in between must be refused by
    the reclaim itself, with nothing deleted, rather than skipped while the untagged versions go."""
    real = maintenance._refuse_pinned_versions
    calls: list[int] = []

    def tag_arrives_after_the_check(ds: Any, branch: str | None, targets: Any) -> None:  # noqa: ANN401 — the pylance handle
        calls.append(1)
        if len(calls) == 1:
            ds.tags.create("late", 2)
            return
        real(ds, branch, targets)

    monkeypatch.setattr(maintenance, "_refuse_pinned_versions", tag_arrives_after_the_check)

    answer = _delete(client, (1, 3))

    assert answer.status_code == 409, answer.text
    assert answer.json()["code"] == 19
    assert "late" in answer.json()["detail"]
    assert _versions(ns) == [1, 2, 3, 4], "the untagged version 1 was deleted while the tagged one was refused"


def test_one_refused_range_deletes_nothing_from_the_others(client: TestClient, ns: LanceNamespace) -> None:
    answer = _delete(client, (1, 2), (LATEST, LATEST + 1))

    assert answer.status_code == 400, answer.text
    assert _versions(ns) == [1, 2, 3, 4], "the valid range was applied before the invalid one was refused"


def test_a_branch_delete_removes_the_branch_version_and_leaves_main(client: TestClient, ns: LanceNamespace) -> None:
    open_dataset(ns, {}, TABLE).create_branch(BRANCH, LATEST)
    _append(ns, 50, branch=BRANCH)
    _append(ns, 51, branch=BRANCH)
    assert _versions(ns, branch=BRANCH) == [4, 5, 6]

    answer = _delete(client, (5, 6), branch=BRANCH)

    assert answer.status_code == 200, answer.text
    assert answer.json()["deleted_count"] == 1
    assert _versions(ns, branch=BRANCH) == [4, 6]
    assert _versions(ns) == [1, 2, 3, 4], "a branch-scoped delete reached main"


def test_a_table_another_dataset_resolves_its_files_through_is_refused(client: TestClient, ns: LanceNamespace, tmp_path: Path) -> None:
    """The reclaim deletes data files only the removed versions referenced, and a shallow clone of this
    table may be the one still reading them — the #114 guard `maintenance/run` applies, applied here.

    The clone is in its base record, as the catalog records a clone it made: an unrecorded one would be
    a base nothing sanctioned, which protects nothing ([[LH-279]])."""
    source = open_dataset(ns, {}, TABLE)
    clone = str(Path(source.uri).parent / "clone.lance")
    source.shallow_clone(clone, reference=2)
    base_registry.claim_bases(
        base_registry.BaseRegistry(control_root=str(tmp_path / "control")),
        clone,
        [
            base_registry.RecordedBase(
                path=source.uri, role=base_registry.BaseRole.DERIVED_FROM, is_dataset_root=True, origin=base_registry.BaseOrigin.SILVER, source_table=source.uri
            )
        ],
    )

    answer = _delete(client, (1, 2))

    assert answer.status_code == 406, answer.text
    assert "resolves its files through" in answer.json()["detail"]
    assert _versions(ns) == [1, 2, 3, 4]
