"""A stage re-run writes what its upstream holds NOW, keyed on `id`, through the handle it decided on ([[LH-213]]).

An unchanged `_rowid` says nothing about unchanged content: a row corrected in place keeps its
stable row id and moves its `_row_last_updated_at_version` (`lance_docs/file_format.md:3989-4015`,
`4270-4298`). So a re-run that compares row identity and skips the write leaves the downstream tier
describing rows that no longer exist upstream, under the lineage of the run that wrote them first.

A column this run adds is joined on `id` and written through the same handle that read the tier's
schema. Lance refuses a Merge whose read version another commit has passed (measured on pylance
12.0.0: "This Merge transaction was preempted by concurrent transaction Update"), so a tier that
moves under the write is re-read, never written by position.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import lance
import pytest

from lineage_kit.consume import DatasetRef, LineageDoc
from lineage_kit.schemas import JobRef
from medallion.services.compute import seed_bronze, transform_stage


def _doc(run_id: str) -> LineageDoc:
    return LineageDoc(
        run_id=run_id,
        job=JobRef(namespace="medallion", name="silver"),
        event_time="2026-10-05T00:00:00Z",
        event_type="COMPLETE",
        producer="https://example.invalid/rask",
        output=DatasetRef(namespace="acme-silver", name="events"),
    )


_FIRST_RUN = "9f1d0d2e-0000-4000-8000-000000000001"
_SECOND_RUN = "9f1d0d2e-0000-4000-8000-000000000002"


def _by_id(uri: str, column: str, version: int | None = None) -> dict[int, Any]:
    table = lance.dataset(uri, version=version).to_table(columns=["id", column])
    return dict(zip(table.column("id").to_pylist(), table.column(column).to_pylist(), strict=True))


#: What a tier armed out of band carries ([[LH-245]]): one commit on it would delete every older version.
_ARMED: dict[str, str | None] = {"lance.auto_cleanup.interval": "1", "lance.auto_cleanup.older_than": "0s", "lance.auto_cleanup.retain_versions": "1"}


@pytest.mark.parametrize("armed", [pytest.param(False, id="plain"), pytest.param(True, id="armed-tier")])
def test_a_payload_corrected_in_place_reaches_silver_under_the_run_that_carried_it(tmp_path: Path, armed: bool) -> None:
    """The armed case: the in-process lane commits with a static key outside the catalog, so it disarms the tier
    itself before its first commit, and the re-run keeps every version silver had ([[LH-245]])."""
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    seed_bronze(bronze, {}, rows=3)
    transform_stage(bronze, silver, {}, stage="silver", lineage=_doc(_FIRST_RUN))
    # In place, on the last row: it keeps its stable `_rowid` and its position, so silver's
    # `source_rowid` list is unchanged element for element.
    lance.dataset(bronze).update({"payload": "'event-2-corrected'"}, where="id = 2")
    if armed:
        lance.dataset(silver).update_config(_ARMED)
    kept = {v["version"] for v in lance.dataset(silver).versions()}

    transform_stage(bronze, silver, {}, stage="silver", lineage=_doc(_SECOND_RUN))

    assert _by_id(silver, "payload") == {0: "event-0", 1: "event-1", 2: "event-2-corrected"}
    assert {json.loads(cell)["run_id"] for cell in _by_id(silver, "lineage").values()} == {_SECOND_RUN}
    assert kept <= {v["version"] for v in lance.dataset(silver).versions()}, "the stage run deleted silver's versions"


def test_a_new_column_lands_on_its_own_rows_when_the_tier_moves_under_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The upstream gains a per-row column while silver's rows have moved, and move again mid-write.

    Every version that holds the column is read, not only the last: a time-travel reader of an
    intermediate commit sees whatever that commit filed, whatever a later commit repaired.
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    seed_bronze(bronze, {}, rows=3)
    transform_stage(bronze, silver, {}, stage="silver", lineage=_doc(_FIRST_RUN))
    lance.dataset(bronze).add_columns({"score": "id * 10"})
    # An update rewrites the row at the end of the tier: silver's physical order is now 1, 2, 0.
    lance.dataset(silver).update({"stage": "'silver'"}, where="id = 0")

    real_merge: Callable[..., Any] = lance.LanceDataset.merge
    moved: list[int] = []

    def merge_after_a_concurrent_commit(self: lance.LanceDataset, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if not moved:
            moved.append(1)
            lance.dataset(silver).update({"stage": "'silver'"}, where="id = 1")
        return real_merge(self, *args, **kwargs)

    monkeypatch.setattr(lance.LanceDataset, "merge", merge_after_a_concurrent_commit)

    transform_stage(bronze, silver, {}, stage="silver", lineage=_doc(_SECOND_RUN))

    holding = [v["version"] for v in lance.dataset(silver).versions() if "score" in lance.dataset(silver, version=v["version"]).schema.names]
    assert holding, "the run never added the column"
    assert {version: _by_id(silver, "score", version) for version in holding} == {version: {0: 0, 1: 10, 2: 20} for version in holding}
