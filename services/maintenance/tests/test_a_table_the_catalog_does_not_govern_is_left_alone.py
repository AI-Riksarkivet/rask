"""A dataset addressed to a table the catalog does not govern is left alone: not paged and not cleaned.

Driven through the CATALOG'S OWN APP over a real `dir` namespace (respx hands each vend request to it),
so the 404 the sweep classifies is the door's own answer. Two ways a swept dataset names such an id:

* its `lineage.dataset_id` stamp names a table nobody registered ([[LH-176]] measured such datasets live);
* its table is dropped into the trash after the tick planned it. The drop deregisters and leaves the
  bytes in place, and a trashed dataset is frozen until undrop or purge: a version cleanup would destroy
  the history an undrop restores.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pytest
import respx
from fastapi.testclient import TestClient

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.expiry import DatasetResult
from maintenance.services.plan import DatasetPlan
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


CATALOG = "http://catalog.test"
#: Keep only the newest version, so a cleanup that runs is visible as a version count.
CLEANUP_EVERYTHING = DatasetPlan(older_than=timedelta(0), retain_versions=1)


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "service_headers", lambda _settings: {})


def _execute(catalog: TestClient, maintain_one: Callable[..., DatasetResult], uri: str) -> tuple[DatasetResult, list[tuple[int, object]]]:
    """One dataset's pass with every vend request answered by the catalog app; returns the result and the door's answers."""
    answers: list[tuple[int, object]] = []

    def _forward(request: httpx.Request) -> httpx.Response:
        answered = catalog.post(request.url.raw_path.decode(), content=request.content, headers={"content-type": "application/json"})
        answers.append((answered.status_code, answered.json().get("code")))
        return httpx.Response(answered.status_code, content=answered.content, headers={"content-type": answered.headers["content-type"]})

    settings = MaintenanceSettings.model_validate({"s3_bucket": "lake", "catalog_url": CATALOG, "min_versions_to_expire": 1})
    with respx.mock() as router:
        router.post(url__startswith=f"{CATALOG}/management/v1/table/").mock(side_effect=_forward)
        result = maintain_one(uri, settings, plan=CLEANUP_EVERYTHING)
    return result, answers


def _assert_left_alone(result: Any, uri: str, refused_added: list[tuple[int, dict[str, Any] | None]]) -> None:
    assert (result.error, result.refused_by) == (None, "table_not_governed"), (result.error, result.error_type, result.refused_by)
    assert result.refused, f"the refusal carries no reason: {result.refused!r}"
    assert refused_added == [(1, {"refused_by": "table_not_governed"})], f"the refusal did not reach the refused counter: {refused_added}"
    assert len(lance.dataset(uri).versions()) == 6, "a table the catalog does not govern had its versions cleaned up"


def test_a_stamp_naming_an_unregistered_table_is_left_alone(
    *, catalog: TestClient, tmp_path: Path, maintain_one: Callable[..., DatasetResult], refused_added: list[tuple[int, dict[str, Any] | None]]
) -> None:
    uri = str(tmp_path / "loose" / "ghost.lance")
    for i in range(6):
        rows = pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())}).replace_schema_metadata({b"lineage.dataset_id": b"acme-bronze$ghost"})
        lance.write_dataset(rows, uri, mode="create" if i == 0 else "append")

    result, answers = _execute(catalog=catalog, maintain_one=maintain_one, uri=uri)

    assert answers == [(404, 1)], f"the door did not answer NamespaceNotFound: {answers}"
    _assert_left_alone(result, uri, refused_added)


def test_a_table_dropped_into_the_trash_after_planning_is_left_alone(
    catalog: TestClient, maintain_one: Callable[..., DatasetResult], refused_added: list[tuple[int, dict[str, Any] | None]]
) -> None:
    table_id = "acme-bronze$events"
    assert catalog.post("/v1/namespace/acme-bronze/create", json={}).status_code == 200
    created = catalog.post(
        f"/v1/table/{table_id}/create",
        content=encode_arrow_stream(pa.table({"id": pa.array(range(10), pa.int64())})),
        headers={"content-type": ARROW_STREAM_MEDIA_TYPE},
    )
    assert created.status_code == 200, created.text
    uri = str(catalog.post(f"/v1/table/{table_id}/describe", json={}).json()["location"])
    for i in range(1, 6):
        lance.write_dataset(pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())}), uri, mode="append")

    dropped = catalog.post(f"/v1/table/{table_id}/drop", json={})
    assert dropped.status_code == 200, dropped.text
    assert len(lance.dataset(uri).get_fragments()) == 6, "the drop deleted the bytes, so this is not the trash case"
    result, answers = _execute(catalog=catalog, maintain_one=maintain_one, uri=uri)

    assert answers == [(404, 4)], f"the door did not answer TableNotFound: {answers}"
    _assert_left_alone(result, uri, refused_added)
