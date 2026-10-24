"""The change-feed ROUTE refuses a bad request before it starts streaming, not as a truncated 200.

The feed streams its answer, so anything that fails after the first byte reaches the caller as a 200
carrying a broken Arrow file. What the scan rejects must therefore be rejected while the endpoint can
still answer with a status. That the stream holds the pod's memory down through the middleware the
service ships is measured from outside a real catalog process by
`tests/integration/test_a_wide_read_stays_under_the_catalog_memory_limit.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, LanceNamespace

from catalog.api.dependencies import get_namespace, get_settings, get_storage_options
from catalog.api.v1.endpoints import data as door
from catalog.core.config import Settings
from catalog.services.dataplane import create_table
from service_kit.lakehouse.ns_errors import install_problem_handlers


_ROWS = 10


@pytest.fixture
def namespace(tmp_path: Path) -> LanceNamespace:
    """``db$t``, ten rows, on a real ``dir`` namespace."""
    ns = lance_namespace.connect("dir", {"root": str(tmp_path / "data")})
    ns.create_namespace(CreateNamespaceRequest(id=["db"]))
    create_table(ns, {}, ["db", "t"], pa.table({"id": pa.array(range(_ROWS), pa.int64())}), root=str(tmp_path / "data"), mode="create", registry=None)
    return ns


@pytest.fixture
def client(namespace: LanceNamespace, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_CONTROL_ROOT=str(tmp_path / "control"))
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.management_router)
    application.state.dapr_client = None

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: namespace
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application) as test_client:
        yield test_client


def test_a_COLUMN_THE_TABLE_DOES_NOT_HAVE_is_a_4xx_and_not_a_truncated_200(client: TestClient) -> None:
    """The reason the first batch is pulled inside `caller_sql` rather than inside the generator.

    MEASURED on the installed pylance: `dataset.scanner(columns=["not_a_column"])` CONSTRUCTS without
    complaint and raises `ValueError: Schema error: No field named not_a_column` only when something
    pulls a batch. `columns` is caller-supplied on this route, so that is a reachable request — and a
    generator's body does not run until the response has already begun. Pulled late it would be a 200
    carrying a few bytes that never open; pulled inside the guard it is a refusal the caller can act on.

    An inverted version window is deliberately NOT the case under test here: `changes.change_filter`
    refuses that before the scan is built, so it would pass with or without the guard and prove
    nothing about where the pull happens.
    """
    response = client.post(
        "/management/v1/table/db$t/changes",
        json={"begin_version": 0, "kind": "inserted", "columns": ["not_a_column"]},
    )

    assert 400 <= response.status_code < 500, f"an unknown column answered {response.status_code}, not a client error"
