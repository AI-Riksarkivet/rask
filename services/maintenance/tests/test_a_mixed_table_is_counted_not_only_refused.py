"""A table whose data files mix Lance file versions must reach its own series, not only the refusal count.

pylance 12 commits an append whose files are at another version than the table's and stamps reader
flag 256, which no operation removes. pylance 11 and lancedb 0.34 (lance core 8) cannot open such a
table. The sweep refuses it on `manifest_flags` like any other unsupported flag, and measured
2026-09-24 45.3-46.5% of every sweep is already refused, so one more refusal moves no alert.

The sweep opens every planned dataset each tick, which makes it the one place a mix is visible
estate-wide. So expiry reads the table's version and the flag before any gate returns, and each dataset's
pass records them, with a per-tick zero so the series exists before the first mix.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import lance
import pyarrow as pa
import pytest

from maintenance.core import metrics
from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials, expiry, sweep
from maintenance.services.expiry import DatasetResult, ExpiryPolicy, ExpiryTarget, expire_versions
from service_kit.lakehouse.features import manifest_feature_flags, mixes_data_file_versions


def _ids(start: int, stop: int) -> pa.Table:
    return pa.table({"id": pa.array(range(start, stop), pa.int64())})


def _table(root: Path, name: str, *, append_version: Literal["2.2"] | None) -> str:
    """A 2.1 + stable-row-id table of four fragments, plus one append at ``append_version``."""
    uri = str(root / name)
    lance.write_dataset(_ids(0, 8), uri, data_storage_version="2.1", enable_stable_row_ids=True, max_rows_per_file=2)
    lance.write_dataset(_ids(8, 10), uri, mode="append", data_storage_version=append_version)
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


@pytest.mark.parametrize(("flags", "mixed"), [(258, True), (64, False)])
def test_a_pod_that_cannot_open_the_table_still_counts_the_mix(monkeypatch: pytest.MonkeyPatch, flags: int, mixed: bool) -> None:
    """A pylance-11 pod refuses to open a flag-256 table; the flags it names are the only evidence it has."""

    def _refuse(*_a: object, **_k: object) -> None:
        raise ValueError(f"Not supported: This dataset cannot be read by this version of Lance. Flags: {flags}")

    monkeypatch.setattr(expiry.lance, "dataset", _refuse)

    result = expire_versions(ExpiryTarget(uri="s3://b/t.lance", storage_options={}), ExpiryPolicy(older_than=None))

    assert result.refused_by == "manifest_flags", f"the open refusal was not read as a refusal: {result}"
    assert result.mixed_data_file_versions is mixed


@pytest.mark.parametrize(("fixture", "expected"), [("mixed_table", 1), ("clean_table", 0)])
def test_each_dataset_records_whether_its_table_is_mixed(
    *,
    request: pytest.FixtureRequest,
    maintain_one: Callable[..., DatasetResult],
    mixed_added: list[tuple[int, dict[str, Any] | None]],
    fixture: str,
    expected: int,
) -> None:
    """The per-dataset call site, driven through the real pass onto a real table."""
    uri: str = request.getfixturevalue(fixture)

    maintain_one(uri, _settings())

    assert mixed_added == [(expected, None)], f"the unit did not record its table's mix: {mixed_added}"


@pytest.mark.parametrize(("fixture", "expected"), [("mixed_table", 1), ("clean_table", 0)])
def test_a_dataset_refused_its_credential_records_the_mix_and_only_the_mix(
    *,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    maintain_one: Callable[..., DatasetResult],
    mixed_added: list[tuple[int, dict[str, Any] | None]],
    fixture: str,
    expected: int,
) -> None:
    """A missing grant stops the dataset before expiry opens it. The probe's own open still reports a mix, and a
    refused CLEAN table records zero, so the counter follows flag 256 rather than the refusal."""
    uri: str = request.getfixturevalue(fixture)

    def _deny(*_a: object, **_k: object) -> dict[str, str]:
        raise credentials.MaintenanceDenied("no write grant on this table")

    monkeypatch.setattr(sweep.credentials, "write_options_for", _deny)

    maintain_one(uri, _settings())

    assert mixed_added == [(expected, None)], f"the pass recorded {mixed_added} for a {fixture}"


def test_a_pod_that_cannot_open_the_table_counts_the_mix_on_the_vend_denied_path(
    monkeypatch: pytest.MonkeyPatch, maintain_one: Callable[..., DatasetResult], mixed_added: list[tuple[int, dict[str, Any] | None]]
) -> None:
    """pylance 11 refuses the open over flag 256; the probe reads the flags from that refusal."""

    def _refuse(*_a: object, **_k: object) -> object:
        raise ValueError("Not supported: This dataset cannot be read by this version of Lance. Please upgrade Lance to read this dataset.\n Flags: 258")

    def _deny(*_a: object, **_k: object) -> dict[str, str]:
        raise credentials.MaintenanceDenied("no write grant on this table")

    monkeypatch.setattr(sweep.lance, "dataset", _refuse)
    monkeypatch.setattr(sweep.credentials, "write_options_for", _deny)

    maintain_one("/nowhere/t.lance", _settings())

    assert mixed_added == [(1, None)], f"a mixed table this pod cannot open went uncounted: {mixed_added}"


def test_the_outcome_record_carries_the_census_fields(mixed_table: str, maintain_one: Callable[..., DatasetResult], caplog: pytest.LogCaptureFixture) -> None:
    """The per-dataset line is the census: it names each table's version and whether it mixes."""
    with caplog.at_level(logging.INFO, logger="maintenance.services.sweep"):
        maintain_one(mixed_table, _settings())

    outcome = next((r for r in caplog.records if r.message == "maintenance_dataset_outcome"), None)
    assert outcome is not None, f"no outcome record: {[r.message for r in caplog.records]}"
    assert getattr(outcome, "data_storage_version", None) == "2.1"
    assert getattr(outcome, "mixed_data_file_versions", None) is True
