"""A governed tier that carries no provenance must not publish.

Owner ruling D1 (2026-08-31): honest `source_rowid` provenance is MANDATORY. The deciding case is
impact analysis — one document is corrupted at ingest, and "which silver and gold rows are
contaminated, so I re-run only those?" must not answer confidently and wrongly.

Three columns make a governed row traceable (`TIER_COLUMNS`): `stage` says which tier it is in,
`lineage` carries the run that produced it, and `source_rowid` names the bronze row it descends from.
The platform verifies NONE of them at publish today, and the omission is not theoretical:
`runners/dummy` is a declarable, baked, accepted lane whose silver schema is
`{id, source_rowid: int64, checksum, word_count, embedding}` — no `stage`, no `lineage`, and the
wrong width for the one provenance column it does carry (the platform mints `uint64`).

The job-side check cannot cover this, and that is the load-bearing detail. `_assert_stage_contract`
refuses parentless rows — but it counts them as
``out.count_rows(filter="source_rowid IS NULL") if SOURCE_ROWID_COLUMN in out.schema.names else 0``.
A table that omits the column entirely therefore reports ZERO parentless rows and passes. The
contract is bypassed by dropping the column it exists to check, which is exactly what a second
executor did.

So the check belongs at PUBLISH, and it is deliberately STRUCTURAL rather than referential: `publish`
is handed a `table_id` and a `version` and does not know the parent table, so "does every value name a
real parent row?" cannot be asked here without new plumbing. It does not need to be — the referential
half already lives in the job, where both datasets are open. Structure is the half that is missing,
and structure alone catches every violation shipping today.

Driven on the wire against a real `dir` namespace and real pylance writes, like its sibling suites:
the subject is what publish does to a real dataset.
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

from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep, StorageOptionsDep, get_lineage_emitter
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints import publication as door
from catalog.services.dataplane import create_table
from catalog.services.publication import published_version
from service_kit.lakehouse.ns_errors import install_problem_handlers


lance = pytest.importorskip("lance")

_SUB = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"
TABLE_ID = ["features"]

#: EXACTLY the shape `runners/dummy` writes — the violation that ships today, not an invented one.
DUMMY_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("source_rowid", pa.int64()),  # the platform mints uint64
        pa.field("checksum", pa.string()),
    ]
)

#: What a conforming tier carries.
GOVERNED_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("payload", pa.string()),
        pa.field("stage", pa.string()),
        pa.field("lineage", pa.string()),
        pa.field("source_rowid", pa.uint64()),
    ]
)


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _dummy_shaped() -> bytes:
    return _ipc(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                # FABRICATED: the row's position, not its parent. `runners/dummy` writes
                # `list(range(len(ids)))` when `_rowid` is absent.
                "source_rowid": pa.array([0, 1, 2], pa.int64()),
                "checksum": pa.array(["a", "b", "c"]),
            },
            schema=DUMMY_SCHEMA,
        )
    )


def _governed() -> bytes:
    return _ipc(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "payload": pa.array(["p1", "p2", "p3"]),
                "stage": pa.array(["silver"] * 3),
                "lineage": pa.array(['{"run_id":"r1"}'] * 3),
                "source_rowid": pa.array([11, 12, 13], pa.uint64()),
            },
            schema=GOVERNED_SCHEMA,
        )
    )


def _app(namespace: Any, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> FastAPI:
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    settings = SimpleNamespace(delimiter="$", registry_root=registry_root, storage_options=lambda: {})
    application.dependency_overrides[SettingsDep.__metadata__[0].dependency] = lambda: settings
    application.dependency_overrides[NamespaceDep.__metadata__[0].dependency] = lambda: namespace
    application.dependency_overrides[StorageOptionsDep.__metadata__[0].dependency] = lambda: {}
    application.dependency_overrides[CurrentToken.__metadata__[0].dependency] = lambda: SimpleNamespace(sub=_SUB)
    application.dependency_overrides[FgaClientDep.__metadata__[0].dependency] = lambda: object()
    application.dependency_overrides[ControlEmitterDep.__metadata__[0].dependency] = lambda: None
    application.dependency_overrides[get_lineage_emitter] = lambda: SimpleNamespace(project_for=_project_for)

    async def _noop_emit(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(door, "emit_control", _noop_emit)
    return application


async def _project_for(_top_ns: str) -> str:
    return "acme"


@pytest.fixture
def registry_root(tmp_path: Path) -> str:
    root = tmp_path / "control"
    root.mkdir()
    return str(root)


def _client_over(payload: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> Iterator[TestClient]:
    namespace = connect("dir", {"root": str(tmp_path / "data")})
    create_table(namespace, {}, TABLE_ID, payload, mode="create")
    with TestClient(_app(namespace, monkeypatch, registry_root)) as c:
        yield c, namespace  # type: ignore[misc]


def _publish(client: TestClient, **body: Any) -> Any:  # noqa: ANN401
    return client.post("/v1/table/features/publish", json={"version": 1, **body})


def test_a_tier_with_no_stage_or_lineage_column_is_REFUSED(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> None:
    """The live violation: `runners/dummy`'s shape must not reach `published`."""
    client, namespace = next(_client_over(_dummy_shaped(), tmp_path, monkeypatch, registry_root))

    response = _publish(client, key_column="id")

    assert response.status_code == 400, f"a tier with no provenance columns published cleanly: {response.text}"
    body = response.json()
    assert "stage" in response.text or "lineage" in response.text or "provenance" in response.text.lower(), body
    assert published_version(namespace, {}, TABLE_ID) is None, "the refusal must be fail-closed — nothing may be pointed at"


def test_a_source_rowid_of_the_wrong_TYPE_is_REFUSED(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> None:
    """`int64` where the platform mints `uint64` is a different column wearing the right name.

    Worth its own test because it is the failure a reader is least likely to see: the column is
    present and non-null, so every count-based check passes.
    """
    payload = _ipc(
        pa.table(
            {
                "id": pa.array([1, 2, 3], pa.int64()),
                "payload": pa.array(["p1", "p2", "p3"]),
                "stage": pa.array(["silver"] * 3),
                "lineage": pa.array(['{"run_id":"r1"}'] * 3),
                "source_rowid": pa.array([11, 12, 13], pa.int64()),
            },
            schema=pa.schema(
                [
                    pa.field("id", pa.int64()),
                    pa.field("payload", pa.string()),
                    pa.field("stage", pa.string()),
                    pa.field("lineage", pa.string()),
                    pa.field("source_rowid", pa.int64()),
                ]
            ),
        )
    )
    client, _ = next(_client_over(payload, tmp_path, monkeypatch, registry_root))

    assert _publish(client, key_column="id").status_code == 400


def test_a_CONFORMING_tier_still_publishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_root: str) -> None:
    """The guard that stops the fix becoming 'refuse everything'."""
    client, namespace = next(_client_over(_governed(), tmp_path, monkeypatch, registry_root))

    response = _publish(client, key_column="id")

    assert response.status_code == 200, response.text
    assert response.json()["published"] is True, response.text
    assert published_version(namespace, {}, TABLE_ID) == 1
