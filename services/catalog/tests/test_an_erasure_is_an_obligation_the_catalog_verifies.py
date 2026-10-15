"""[[LH-493]] An erasure is an obligation: the catalog records it, a bring-your-own job rewrites, the catalog verifies.

ADR 0163. The erasure door commits no data; the job deletes, compacts and rebuilds on its own compute; the
verification door judges what the job left on bytes and releases the obligation only when nothing it owes remains.
Driven through the erasure doors on a real pylance ``dir`` backend with the obligations on a local control root. The
job is the conftest's ``ErasureJob``, pylance straight on the table's files: the claims here are what the catalog
records and verifies. A stock client's commits through ``version/create`` are
``test_a_stock_client_commits_every_operation_through_version_create.py``'s claim.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespace

from catalog.api.dependencies import get_clock, get_namespace, get_storage_options
from catalog.api.security import authenticate
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings, get_settings
from catalog.core.namespace import open_dataset
from catalog.services.dataplane import create_table, read_arrow_body
from service_kit.lakehouse.ns_errors import install_problem_handlers


_SUBJECT = "alice"
_PREDICATE = f"pii = '{_SUBJECT}'"
_TABLE_ID = ["subjects"]


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _create(ns: LanceNamespace, table_id: list[str]) -> None:
    """``table_id`` through the catalog's create path: the subject beside bob in one fragment, ``pii`` BTREE-indexed."""
    rows = pa.table({"pii": pa.array(["bob", _SUBJECT])})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    create_table(ns, {}, table_id, read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=1 << 20), mode="create", registry=None)
    open_dataset(ns, {}, table_id).create_scalar_index("pii", "BTREE")


def _subjects(root: Path) -> LanceNamespace:
    ns = lance_namespace.connect("dir", {"root": str(root)})
    _create(ns, _TABLE_ID)
    return ns


@contextmanager
def _catalog(ns: LanceNamespace, control: Path, clock: _Clock) -> Generator[TestClient]:
    """The erasure doors, the owner ``olivia`` signed in, authorization off."""
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_CONTROL_ROOT=str(control))
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: ns
    application.dependency_overrides[get_storage_options] = lambda: {}
    application.dependency_overrides[authenticate] = lambda: SimpleNamespace(sub="olivia")
    application.dependency_overrides[get_clock] = lambda: clock
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def _record(client: TestClient, table: str = "subjects") -> dict[str, Any]:
    response = client.post(f"/management/v1/table/{table}/erasure", json={"predicate": _PREDICATE})
    assert response.status_code == 202, response.text
    return response.json()


def _verify(client: TestClient, obligation_id: str, predicate: str = _PREDICATE) -> Any:  # noqa: ANN401 — an httpx.Response
    return client.post(f"/management/v1/table/subjects/erasure/{obligation_id}/verify", json={"predicate": predicate})


def _view(client: TestClient, obligation_id: str) -> dict[str, Any]:
    """The obligation as the list door shows it."""
    (view,) = [entry for entry in client.get("/management/v1/erasures").json()["obligations"] if entry["obligation_id"] == obligation_id]
    return view


def test_the_door_records_an_obligation_and_commits_nothing(tmp_path: Path) -> None:
    """The door is the requester's act; every rewrite is the job's. Its record keeps no trace of the subject."""
    ns = _subjects(tmp_path / "data")
    location = open_dataset(ns, {}, _TABLE_ID).uri
    history = [entry["version"] for entry in lance.dataset(location).versions()]

    with _catalog(ns, tmp_path / "control", _Clock()) as client:
        obligation = _record(client)

    expected = ("open", {"main": history[-1]}, "user:olivia", [])
    assert (obligation["state"], obligation["refs"], obligation["requested_by"], obligation["open_surfaces"]) == expected
    after = lance.dataset(location)
    assert ([entry["version"] for entry in after.versions()], after.count_rows(filter=_PREDICATE)) == (history, 1)
    recorded = (tmp_path / "control" / "_erasure_obligations" / f"{obligation['obligation_id']}.json").read_bytes()
    assert _SUBJECT.encode() not in recorded


@pytest.mark.parametrize(
    ("steps", "named"),
    [
        pytest.param(("compact", "drop_indices"), "main", id="the-delete"),
        pytest.param(("delete", "drop_indices"), "compact:main", id="the-compaction"),
        pytest.param(("delete", "compact"), "index:main", id="the-index-drop"),
        pytest.param(("delete", "compact", "rebuild_indices"), "index:main", id="an-index-rebuilt-rather-than-dropped"),
    ],
)
def test_a_job_that_skips_a_surface_leaves_the_obligation_open_naming_it(tmp_path: Path, erasure_job: Any, steps: tuple[str, ...], named: str) -> None:  # noqa: ANN401 — conftest.ErasureJob
    """Each step the job owes is judged on the head's bytes, never taken on the job's word: an index it rebuilt is a
    segment committed since the obligation was recorded, whose provenance is the job's own claim, so it proves nothing."""
    ns = _subjects(tmp_path / "data")
    location = open_dataset(ns, {}, _TABLE_ID).uri
    with _catalog(ns, tmp_path / "control", _Clock()) as client:
        obligation = _record(client)
        job = erasure_job(location, _PREDICATE)
        for step in steps:
            getattr(job, step)()
        verified = _verify(client, obligation["obligation_id"])

    assert verified.status_code == 200, verified.text
    standing = verified.json()["obligation"]
    owed = [surface["surface"] for surface in standing["open_surfaces"] if surface["outcome"] == "failed" and surface["surface"] != "verify"]
    assert (standing["state"], owed) == ("open", [named]), standing["open_surfaces"]


type _Refused = Callable[[], Any]


def _another_predicate(client: TestClient, ns: LanceNamespace, erasure_job: Any) -> tuple[str, _Refused]:  # noqa: ANN401 — conftest.ErasureJob
    """A predicate nothing matches would verify clean and release the obligation while the subject stays."""
    obligation = _record(client)["obligation_id"]
    return obligation, lambda: _verify(client, obligation, predicate="pii = 'nobody'")


def _another_tables(client: TestClient, ns: LanceNamespace, erasure_job: Any) -> tuple[str, _Refused]:  # noqa: ANN401 — conftest.ErasureJob
    """An obligation is verified on the table it was recorded on, whatever table the caller may maintain."""
    _create(ns, ["others"])
    obligation = _record(client, "others")["obligation_id"]
    return obligation, lambda: _verify(client, obligation)


def _a_released_one(client: TestClient, ns: LanceNamespace, erasure_job: Any) -> tuple[str, _Refused]:  # noqa: ANN401 — conftest.ErasureJob
    """A released obligation's digest is gone, and nothing reopens it."""
    obligation = _record(client)["obligation_id"]
    erasure_job(open_dataset(ns, {}, _TABLE_ID).uri, _PREDICATE).run()
    assert _verify(client, obligation).json()["obligation"]["state"] == "released"
    return obligation, lambda: _verify(client, obligation)


@pytest.mark.parametrize(
    ("arrange", "answer"),
    [
        pytest.param(_another_predicate, (400, 13), id="a-predicate-other-than-the-obligations"),
        pytest.param(_another_tables, (409, 19), id="another-tables-obligation"),
        pytest.param(_a_released_one, (409, 19), id="a-released-obligation"),
    ],
)
def test_a_verification_the_obligation_does_not_admit_is_refused_and_changes_nothing(
    tmp_path: Path,
    erasure_job: Any,  # noqa: ANN401 — conftest.ErasureJob
    arrange: Callable[[TestClient, LanceNamespace, Any], tuple[str, _Refused]],
    answer: tuple[int, int],
) -> None:
    ns = _subjects(tmp_path / "data")
    with _catalog(ns, tmp_path / "control", _Clock()) as client:
        obligation, attempt = arrange(client, ns, erasure_job)
        before = _view(client, obligation)
        response = attempt()
        after = _view(client, obligation)

    assert (response.status_code, response.headers["content-type"]) == (answer[0], "application/problem+json"), response.text
    assert response.json()["code"] == answer[1]
    assert after == before


def test_an_obligation_open_past_its_deadline_is_listed_overdue_and_nothing_acts_on_it(tmp_path: Path) -> None:
    """A missed deadline is visible on the obligation; the catalog never does the job's work itself."""
    ns = _subjects(tmp_path / "data")
    location = open_dataset(ns, {}, _TABLE_ID).uri
    clock = _Clock()
    with _catalog(ns, tmp_path / "control", clock) as client:
        _record(client)
        clock.now += timedelta(days=31)
        listed = client.get("/management/v1/erasures").json()["obligations"]

    assert [(entry["state"], entry["overdue"]) for entry in listed] == [("open", True)]
    assert lance.dataset(location).count_rows(filter=_PREDICATE) == 1
