"""A branch's announcement names the parent the DATASET recorded, not the one the request asked for.

[[LH-056]]. "Branched from what" is the question a consumer asks next, and the create event answered it
from `CreateTableBranchRequest` — which is empty for the common case. Measured against the installed
pylance:

    main at version 2
    create_branch("plain")                 -> parent_branch=None parent_version=2
    create_branch("explicit", reference=1) -> parent_branch=None parent_version=1

So a branch taken from main with no explicit source records `parent_version=2` while the request
carried `from_version=None`, and the event announced the null. The announcement was LESS informative
than the object it announces, for the case that happens most — and a console that stored it has no way
to learn otherwise without going back to the catalog, which is what the event exists to save it.

READ BACK AFTER THE CREATE, once, because a create is rare and a wrong announcement is permanent. The
request is not a fallback: if the read fails, the fields are omitted rather than filled with values
that may be null for a branch whose parent is recorded.

NAMED `parent_branch`/`parent_version`, matching `list_branches`, because that is what they now carry.
`from_version` already means something else in this estate — ingest's publish delta range
(`ingest/runs.py`) — so reusing it here for a ref ancestor is a collision of vocabulary in an `extra`
bag that has no schema to keep them apart.
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
from lance_namespace import connect

from catalog.api.dependencies import ControlEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import branches as branch_door
from catalog.services.dataplane import create_table
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
TABLE = ["alpha"]
TABLE_PATH = "alpha"


def _table(start: int) -> pa.Table:
    return pa.table({"id": pa.array([start, start + 1, start + 2], pa.int64())})


@pytest.fixture
def ns(tmp_path: Path):  # noqa: ANN201 — LanceNamespace, a runtime-only type
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE, _table(1), mode="create")
    # A SECOND VERSION, so "the parent is main's current version" is a number the request could not
    # have guessed and a null could not stand in for. With one version, 1 and "unset" are too close
    # to tell apart in a failure message.
    lance.write_dataset(pa.table({"id": pa.array([4, 5, 6], pa.int64())}), str(tmp_path / "data" / f"{TABLE_PATH}.lance"), mode="append")
    return namespace


@pytest.fixture
def emitted() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def app(ns, emitted: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:  # noqa: ANN001
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(branch_door.router)

    settings = SimpleNamespace(delimiter="$", storage_options=lambda: {}, fga_enabled=False, registry_root=str(tmp_path / "control"))
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: ns
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: SimpleNamespace(sub=_SUB)
    application.dependency_overrides[ControlEmitterDep.__metadata__[0].dependency] = lambda: None

    async def _record(_emitter: Any, **kwargs: Any) -> None:
        emitted.append(kwargs)

    monkeypatch.setattr(branch_door, "emit_control", _record, raising=False)
    yield application


def _create(client: TestClient, name: str, **source: Any) -> Any:  # noqa: ANN401 — httpx.Response
    return client.post(f"/v1/table/{TABLE_PATH}/branches/create", json={"id": TABLE, "name": name, **source})


def test_a_branch_from_MAIN_announces_the_version_it_actually_branched_from(app: FastAPI, emitted: list[dict[str, Any]]) -> None:
    """The common case, and the one the request cannot answer: no `from_version` was sent, and the
    branch records main's current version."""
    with TestClient(app) as client:
        assert _create(client, "plain").status_code == 200

    extra = emitted[0]["extra"]
    assert extra.get("parent_version") == 2, f"the event does not name the version the branch was taken from: {extra}"
    assert extra.get("parent_branch") is None, f"a branch from main must not name a parent branch: {extra}"
    assert extra.get("branch") == "plain", f"the event no longer names the branch it is about: {extra}"


def test_an_EXPLICIT_source_version_is_announced_as_the_parent(app: FastAPI, emitted: list[dict[str, Any]]) -> None:
    """The control for the leg above: a different number must produce a different announcement, or the
    first test would pass on an implementation that hard-codes the table's head."""
    with TestClient(app) as client:
        assert _create(client, "older", from_version=1).status_code == 200

    assert emitted[0]["extra"].get("parent_version") == 1, f"an explicit source version was not announced: {emitted[0]['extra']}"


def test_a_branch_OF_A_BRANCH_names_that_branch_as_its_parent(app: FastAPI, emitted: list[dict[str, Any]]) -> None:
    """The field that is null for every branch above, so its own leg is the only place it can fail."""
    with TestClient(app) as client:
        assert _create(client, "work").status_code == 200
        assert _create(client, "child", from_branch="work").status_code == 200

    extra = emitted[-1]["extra"]
    assert extra.get("parent_branch") == "work", f"a branch of a branch announces no parent branch: {extra}"


def test_the_REQUEST_is_not_used_as_a_fallback(app: FastAPI, emitted: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A read-back that fails must omit the fields rather than fill them from the request.

    The request's values are what this replaces, and they are null exactly where the answer matters —
    so falling back to them would restore the defect silently on the one path where the authoritative
    read is unavailable. Driven with an EXPLICIT `from_version`, so a fallback would produce a
    plausible number rather than another null and the assertion has something to catch.
    """

    def _blind(*_a: Any, **_k: Any) -> Any:  # noqa: ANN401 — a stand-in that only ever raises
        raise RuntimeError("the branch list is unreadable")

    monkeypatch.setattr(branch_door.dataplane, "list_branches", _blind)
    with TestClient(app) as client:
        assert _create(client, "unreadable", from_version=1).status_code == 200

    extra = emitted[0]["extra"]
    assert "parent_version" not in extra, f"the event fell back to the request, which is the value this replaces: {extra}"
    assert extra.get("branch") == "unreadable", "the announcement itself must survive an unreadable parent"
