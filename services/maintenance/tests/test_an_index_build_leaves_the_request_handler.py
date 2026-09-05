"""An index build is queued work, not a request — docs/DECISIONS.md "Maintenance leaves the planner pod".

`create_index` / `create_scalar_index` ran the whole build inside the catalog's own handler, so the
cost of a request was a property of the TABLE rather than of the request: unbounded work no pod
sizing bounds, because the next table is bigger.

**The spec already asks for the queued form.** `CreateTableIndex` states that "index creation is
handled asynchronously" and that progress is read through `ListTableIndices` /
`DescribeTableIndexStats`; its response carries an optional `transaction_id` and nothing else. So the
door answering with a unit id and returning in milliseconds is spec-conformant, and the synchronous
build was the divergence.

MEASURED before this was built (pylance 10.0.0, 2026-09-04), because it decided the shape: an index
segment — what `create_index_uncommitted` returns — carries no `json`, `to_json` or `serialize`, so
compaction's plan-here / execute-there / commit-here split cannot be spread across processes for an
index today. The WHOLE build crosses to the worker instead, which still takes it off the request path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from maintenance.api import index_work
from maintenance.core.config import MaintenanceSettings
from maintenance.services.index_build import UnknownIndexKindError, build_index
from service_kit.lakehouse.work_items import SCALAR_INDEX, VECTOR_INDEX, IndexWorkItem


def _table(tmp_path: Path, *, rows: int = 256) -> str:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(rows), pa.int64()), "s": [f"v{i}" for i in range(rows)]}), uri)
    return uri


def _settings(**over: object) -> MaintenanceSettings:
    return MaintenanceSettings.model_validate({"s3_bucket": "lake"} | over)


def test_the_worker_builds_the_index_the_unit_describes(tmp_path: Path) -> None:
    """The headline: a unit crossing a broker is everything the build needs."""
    uri = _table(tmp_path)
    item = IndexWorkItem(uri=uri, column="id", kind=SCALAR_INDEX, index_type="BTREE", name="id_idx")

    outcome = build_index(item, write_options={})

    assert outcome.name == "id_idx"
    assert outcome.version >= 2, "the build committed no new version"
    assert [i.name for i in lance.dataset(uri).describe_indices()] == ["id_idx"]


def test_an_unknown_scalar_type_is_REFUSED_before_pylance_sees_it(tmp_path: Path) -> None:
    """A value arriving off a broker is producer-controlled. Narrowing it here makes an unknown type a
    named refusal instead of a `TypeError` deep inside pylance, which reads as a worker crash."""
    item = IndexWorkItem(uri=_table(tmp_path), column="id", kind=SCALAR_INDEX, index_type="NOT_A_TYPE")

    with pytest.raises(UnknownIndexKindError, match="NOT_A_TYPE"):
        build_index(item, write_options={})


def test_an_unknown_KIND_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """Vector and scalar are separate spec operations. A worker inferring the door from `index_type`
    could build a scalar index where a vector one was asked for — a table that answers queries wrongly
    rather than not at all, which is the worse failure and the harder to notice."""
    item = IndexWorkItem(uri=_table(tmp_path), column="id", kind="guess", index_type="BTREE")

    with pytest.raises(UnknownIndexKindError, match="guess"):
        build_index(item, write_options={})


@pytest.mark.asyncio
async def test_a_malformed_unit_is_ACKED_not_retried(tmp_path: Path) -> None:
    """It will not parse on the tenth attempt either. Retrying only delays the DLQ while occupying a
    worker — the rule the compaction lane's own handler already states."""
    assert await index_work.handle_index_unit({"data": {"nope": 1}}, _settings()) == {"status": "SUCCESS"}


@pytest.mark.asyncio
async def test_a_producer_DEFECT_is_acked_and_a_STORE_failure_retries(tmp_path: Path) -> None:
    """The two failures a worker must tell apart.

    An unbuildable unit is a producer defect: no redelivery repairs it, so it is acked and logged. A
    store outage or a mid-build crash is exactly what redelivery is for, so it RETRIES — an index
    build is re-runnable, and pylance refuses a duplicate name rather than silently doubling one.
    """
    uri = _table(tmp_path)
    unbuildable = {"data": IndexWorkItem(uri=uri, column="id", kind="guess", index_type="BTREE").model_dump()}
    assert await index_work.handle_index_unit(unbuildable, _settings()) == {"status": "SUCCESS"}

    missing = {"data": IndexWorkItem(uri=str(tmp_path / "gone.lance"), column="id", kind=SCALAR_INDEX, index_type="BTREE").model_dump()}
    assert await index_work.handle_index_unit(missing, _settings()) == {"status": index_work.RETRY}


@pytest.mark.asyncio
async def test_the_build_is_signed_by_the_TABLE_scoped_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An index build lands files under the table's own prefix, so it is a WRITE and takes the same
    vending path a compaction rewrite does — the whole reason `table_id` rides the unit."""
    uri = _table(tmp_path)
    seen: dict[str, Any] = {}

    def _vend(location: str, settings: Any, *, fallback: dict[str, str], declared_table_id: str | None = None) -> dict[str, str]:
        seen["uri"], seen["table_id"] = location, declared_table_id
        return {"aws_access_key_id": "vended"}

    def _built(item: IndexWorkItem, *, write_options: Any) -> Any:
        seen["write_options"] = dict(write_options)
        from maintenance.services.index_build import IndexOutcome

        return IndexOutcome(name="x", column=item.column, kind=item.kind, version=2)

    monkeypatch.setattr(index_work.credentials, "write_options_for", _vend)
    monkeypatch.setattr(index_work, "build_index", _built)

    item = IndexWorkItem(uri=uri, table_id="acme-bronze$events", column="id", kind=SCALAR_INDEX, index_type="BTREE")
    assert await index_work.handle_index_unit({"data": item.model_dump()}, _settings()) == {"status": "SUCCESS"}

    assert seen["table_id"] == "acme-bronze$events", "the declared id must reach the vending door or it signs with the root key"
    assert seen["write_options"] == {"aws_access_key_id": "vended"}


def test_the_unit_id_is_DETERMINISTIC_so_a_redelivery_is_one_build() -> None:
    """The spec points a caller at `ListTableIndices` to follow progress. An id that changed per
    delivery would make two deliveries of one request look like two builds."""
    kwargs: dict[str, Any] = {"uri": "s3://b/t", "column": "v", "kind": VECTOR_INDEX, "index_type": "IVF_PQ"}
    assert IndexWorkItem(**kwargs).unit_id == IndexWorkItem(**kwargs).unit_id
    assert IndexWorkItem(**kwargs).unit_id != IndexWorkItem(**{**kwargs, "column": "w"}).unit_id


def test_the_lane_is_OFF_unless_a_topic_is_configured() -> None:
    """Nothing consumes a unit nobody subscribes to, so the door must keep building in-process where
    no index topic is set. Both sides read the same topic name for exactly this reason."""
    from fastapi import FastAPI

    assert index_work.register_index_route(FastAPI(), _settings()) is None
    assert index_work.register_index_route(FastAPI(), _settings(index_topic="maintenance.index.v1")) is not None


def test_a_COLUMN_THAT_IS_NOT_IN_THE_SCHEMA_is_acked_not_retried(tmp_path: Path) -> None:
    """A unit naming a column the table does not have is a producer defect, and redelivery cannot
    repair one.

    MEASURED on pylance 10.0.0: `create_scalar_index("nope", …)` raises `KeyError: 'nope not found in
    schema'`. That reached the route's bare `except Exception` and answered RETRY, so the unit came
    back every `ackWait` forever — occupying a worker to fail identically, and burying the store
    outages RETRY exists for.

    Refused as a VALIDATION rather than caught as an exception: the schema is in hand before the build
    starts, so "this column does not exist" is a question with an answer, not a failure to classify.
    Catching `KeyError` would also swallow one raised for an unrelated reason deeper in the build.

    The two sibling shapes the audit named alongside it do NOT exist, measured the same way, and are
    recorded here so they are not re-fixed: a bad kwarg raises nothing at all (pylance ignores unknown
    keyword arguments), and a redelivered unit whose index already exists succeeds — the scalar path
    replaces. Only the column shape was real.
    """
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array(list(range(300)), pa.int64())}), uri)
    item = IndexWorkItem(uri=uri, column="not_a_column", kind=SCALAR_INDEX, index_type="BTREE")

    with pytest.raises(UnknownIndexKindError, match="not_a_column"):
        build_index(item, write_options={})


def test_a_column_that_EXISTS_still_builds(tmp_path: Path) -> None:
    """The guard refuses the absent column and nothing else — a real column is unaffected."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array(list(range(300)), pa.int64())}), uri)

    outcome = build_index(IndexWorkItem(uri=uri, column="id", kind=SCALAR_INDEX, index_type="BTREE"), write_options={})

    assert outcome.column == "id"
