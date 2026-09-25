"""A table whose data files mix Lance file versions must reach its own series, not only the refusal count.

pylance 12 commits an append whose files are at another version than the table's and stamps reader
flag 256, which no operation removes. pylance 11 and lancedb 0.34 (lance core 8) cannot open such a
table. The sweep refuses it on `manifest_flags` like any other unsupported flag, and measured
2026-09-24 45.3-46.5% of every sweep is already refused, so one more refusal moves no alert.

The sweep opens every planned dataset each tick, which makes it the one place a mix is visible
estate-wide. So `compact_one` reads the table's version and the flag before any gate returns, and both
lanes record them per dataset, with a per-tick zero so the series exists before the first mix.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from maintenance.core import metrics
from maintenance.core.config import MaintenanceSettings
from maintenance.services import compaction_executor, optimize, sweep
from maintenance.services.optimize import compact_one
from service_kit.lakehouse import base_refs
from service_kit.lakehouse.features import manifest_feature_flags, mixes_data_file_versions
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem


def _ids(start: int, stop: int) -> pa.Table:
    return pa.table({"id": pa.array(range(start, stop), pa.int64())})


def _table(root: Path, name: str, *, append_version: str | None) -> str:
    """A 2.1 + stable-row-id table of four fragments, plus one append at ``append_version``."""
    uri = str(root / name)
    lance.write_dataset(_ids(0, 8), uri, data_storage_version="2.1", enable_stable_row_ids=True, max_rows_per_file=2)
    lance.write_dataset(_ids(8, 10), uri, mode="append", **({"data_storage_version": append_version} if append_version else {}))
    return uri


@pytest.fixture
def mixed_table(tmp_path: Path) -> str:
    uri = _table(tmp_path, "mixed.lance", append_version="2.2")
    reader, _ = manifest_feature_flags(lance.dataset(uri))
    assert mixes_data_file_versions(reader), f"pylance did not set flag 256 (reader={reader}), so nothing here is tested"
    return uri


@pytest.fixture
def clean_table(tmp_path: Path) -> str:
    uri = _table(tmp_path, "clean.lance", append_version=None)
    reader, _ = manifest_feature_flags(lance.dataset(uri))
    assert not mixes_data_file_versions(reader), f"an inheriting append set flag 256 (reader={reader})"
    return uri


@pytest.fixture
def mixed_added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any] | None]]:
    seen: list[tuple[int, dict[str, Any] | None]] = []
    monkeypatch.setattr(metrics._mixed_file_versions, "add", lambda amount, attributes=None, **_: seen.append((amount, attributes)))
    return seen


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings.model_validate({"s3_bucket": "b"})


def test_a_mixed_table_is_named_as_mixed_as_well_as_refused(mixed_table: str) -> None:
    result = compact_one(mixed_table, {}, None)

    assert result.refused_by == "manifest_flags", f"a flag-256 table was not refused: {result}"
    assert result.mixed_data_file_versions is True, f"the refusal did not say the table mixes file versions: {result}"
    assert result.data_storage_version == "2.1", f"the table's own version was not reported: {result.data_storage_version!r}"


def test_a_clean_table_reports_its_version_and_no_mix(clean_table: str) -> None:
    result = compact_one(clean_table, {}, None)

    assert result.refused is None and result.error is None, f"the control table was not maintained: {result}"
    assert result.mixed_data_file_versions is False
    assert result.data_storage_version == "2.1"


def test_the_version_is_reported_at_the_protected_base_exit(clean_table: str) -> None:
    """The census field rides every exit that opened the dataset, including the one before any rewrite."""
    protected = base_refs.BaseRefs(protected={base_refs.normalise(clean_table)})

    result = compact_one(clean_table, {}, None, protected=protected)

    assert result.refused_by == "protected_base", f"the fixture did not reach the protected-base exit: {result}"
    assert result.data_storage_version == "2.1"


@pytest.mark.parametrize(("flags", "mixed"), [(258, True), (64, False)])
def test_a_pod_that_cannot_open_the_table_still_counts_the_mix(monkeypatch: pytest.MonkeyPatch, flags: int, mixed: bool) -> None:
    """A pylance-11 pod refuses to open a flag-256 table; the flags it names are the only evidence it has."""

    def _refuse(*_a: object, **_k: object) -> None:
        raise ValueError(f"Not supported: This dataset cannot be read by this version of Lance. Flags: {flags}")

    monkeypatch.setattr(optimize.lance, "dataset", _refuse)

    result = compact_one("s3://b/t.lance", {}, None)

    assert result.refused_by == "manifest_flags", f"the open refusal was not read as a refusal: {result}"
    assert result.mixed_data_file_versions is mixed


@pytest.mark.parametrize(("fixture", "expected"), [("mixed_table", 1), ("clean_table", 0)])
def test_each_unit_records_whether_its_table_is_mixed(
    request: pytest.FixtureRequest, mixed_added: list[tuple[int, dict[str, Any] | None]], fixture: str, expected: int
) -> None:
    """The per-dataset call site, driven through the real unit path onto a real table."""
    uri: str = request.getfixturevalue(fixture)

    sweep.execute_unit(DatasetWorkItem(uri=uri, plan=DatasetPlan()), settings=_settings(), options={}, now=datetime.now(UTC))

    assert mixed_added == [(expected, None)], f"the unit did not record its table's mix: {mixed_added}"


@pytest.mark.parametrize(("fixture", "expected"), [("mixed_table", 1), ("clean_table", 0)])
def test_a_unit_refused_its_credential_records_the_mix_and_only_the_mix(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    mixed_added: list[tuple[int, dict[str, Any] | None]],
    fixture: str,
    expected: int,
) -> None:
    """A missing write grant stops the unit before compact_one. The probe's own open still reports a mix, and a
    refused CLEAN table records zero, so the counter follows flag 256 rather than the refusal."""
    uri: str = request.getfixturevalue(fixture)

    def _deny(*_a: object, **_k: object) -> dict[str, str]:
        raise compaction_executor.MaintenanceDenied("no write grant on this table")

    monkeypatch.setattr(sweep.credentials, "write_options_for", _deny)

    sweep.execute_unit(DatasetWorkItem(uri=uri, plan=DatasetPlan()), settings=_settings(), options={}, now=datetime.now(UTC))

    assert mixed_added == [(expected, None)], f"the unit recorded {mixed_added} for a {fixture}"


def test_a_pod_that_cannot_open_the_table_counts_the_mix_on_the_vend_denied_path(
    monkeypatch: pytest.MonkeyPatch, mixed_added: list[tuple[int, dict[str, Any] | None]]
) -> None:
    """pylance 11 refuses the open over flag 256; the probe reads the flags from that refusal."""

    def _refuse(*_a: object, **_k: object) -> object:
        raise ValueError("Not supported: This dataset cannot be read by this version of Lance. Please upgrade Lance to read this dataset.\n Flags: 258")

    def _deny(*_a: object, **_k: object) -> dict[str, str]:
        raise compaction_executor.MaintenanceDenied("no write grant on this table")

    monkeypatch.setattr(sweep.lance, "dataset", _refuse)
    monkeypatch.setattr(sweep.credentials, "write_options_for", _deny)

    sweep.execute_unit(DatasetWorkItem(uri="/nowhere/t.lance", plan=DatasetPlan()), settings=_settings(), options={}, now=datetime.now(UTC))

    assert mixed_added == [(1, None)], f"a mixed table this pod cannot open went uncounted: {mixed_added}"


def test_each_tick_emits_the_zero_baseline(monkeypatch: pytest.MonkeyPatch, mixed_added: list[tuple[int, dict[str, Any] | None]]) -> None:
    """An estate whose planner enqueues nothing runs no unit, so the per-tick call is the only emission."""
    monkeypatch.setattr(sweep, "_load_policies", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep, "_trash_exclusions", lambda *_a, **_k: {})
    monkeypatch.setattr(sweep, "_s3fs", lambda *_a, **_k: object())
    monkeypatch.setattr(sweep, "_buckets_to_sweep", lambda *_a, **_k: ["b"])
    monkeypatch.setattr(sweep, "_discover_all", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep, "_protected_roots", lambda *_a, **_k: base_refs.BaseRefs())

    items, decided = sweep.plan_sweep(_settings())

    assert (items, decided) == ([], []), "the fixture planned work, so the zero below could come from a unit"
    assert mixed_added == [(0, None)], f"the tick emitted no zero baseline: {mixed_added}"


def test_the_outcome_record_carries_the_census_fields(mixed_table: str, caplog: pytest.LogCaptureFixture) -> None:
    """The per-dataset line is the census: it names each table's version and whether it mixes."""
    with caplog.at_level(logging.INFO, logger="maintenance.services.sweep"):
        sweep.execute_unit(DatasetWorkItem(uri=mixed_table, plan=DatasetPlan()), settings=_settings(), options={}, now=datetime.now(UTC))

    outcome = next((r for r in caplog.records if r.message == "maintenance_dataset_outcome"), None)
    assert outcome is not None, f"no outcome record: {[r.message for r in caplog.records]}"
    assert getattr(outcome, "data_storage_version", None) == "2.1"
    assert getattr(outcome, "mixed_data_file_versions", None) is True
