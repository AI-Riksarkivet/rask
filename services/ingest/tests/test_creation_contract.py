"""A14 — the creation contract must REFUSE, and the refusal must be provable.

*"Every governed dataset is created with `enable_stable_row_ids=True` and `id` declared as the
unenforced primary key; the catalog's creation contract refuses datasets missing either; a test
proves the refusal."*

The refusal is the whole gate. Both guarantees are creation-time-only — setting
`enable_stable_row_ids` afterwards is a documented silent no-op
(`file_format.md:4011-4013 + guide.md:228-229`) — so a dataset created without them works perfectly, reports
green, and fails months later in someone else's job as a stage runner that duplicates rows or a
`source_rowid` that resolves to the wrong page. There is no symptom at the moment the mistake is
made, which is exactly why it needs a gate rather than a convention.

These run against REAL Lance, not a double. A gate over a mock would assert that the mock refuses.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest

from ingest.catalog import CreationContractError, LocalCatalog, assert_creation_contract
from ingest.lander import create_empty
from service_kit.lakehouse.features import manifest_feature_flags, mixes_data_file_versions, unsupported_features


BRONZE = pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), pa.field("payload", pa.binary())])


def _bronze_rows(ids: list[int]) -> pa.Table:
    return pa.table({"id": ids, "source_uri": [f"file:///{i}" for i in ids], "payload": [b"x"] * len(ids)}, schema=BRONZE)


def test_ensure_at_ENFORCES_the_contract_rather_than_merely_offering_it(tmp_path: Path) -> None:
    """The gate must sit on the path a run actually takes.

    A dataset that pre-dates the contract, or that some other writer created, reaches `ensure_at`
    like any other — and because it already exists, the creation branch is skipped entirely. If the
    check only ran at creation it would never see the datasets most likely to be wrong.
    """
    uri = str(tmp_path / "legacy.lance")
    lance.write_dataset(BRONZE.empty_table(), uri, mode="create", data_storage_version="2.2")

    with pytest.raises(CreationContractError):
        LocalCatalog(BRONZE).ensure_at(uri)


def test_the_gate_REFUSES_when_the_accessor_is_missing(tmp_path: Path) -> None:
    """False-on-absence: a pylance upgrade must not silently retire the gate.

    `has_stable_row_ids` is read by name. If a future version renames it, an optimistic default
    would make every dataset pass and A14 would stop gating with no test failing anywhere — the
    quietest way a guarantee can disappear.
    """
    from ingest.catalog import _has_stable_row_ids

    class _NoAccessor:
        pass

    assert _has_stable_row_ids(_NoAccessor()) is False


def test_a14_refuses_a_table_that_already_mixes_file_versions(tmp_path: Path) -> None:
    """Reader flag 256 is sticky: maintenance refuses the table and pylance 11 and lancedb 0.34 cannot open it.

    A run into it only grows what the one measured remedy, recreating into a new dataset, has to copy.
    """
    uri = str(tmp_path / "mixed.lance")
    lance.write_dataset(_bronze_rows([1]), uri, mode="create", data_storage_version="2.1", enable_stable_row_ids=True)
    lance.write_dataset(_bronze_rows([2]), uri, mode="append", data_storage_version="2.2")
    assert mixes_data_file_versions(manifest_feature_flags(lance.dataset(uri))[0]), "the fixture no longer builds a mixed table on this pylance"

    with pytest.raises(CreationContractError, match="256"):
        assert_creation_contract(uri)


def test_a14_accepts_ingests_own_externally_based_create(tmp_path: Path) -> None:
    """A14 asks for bit 256 alone, because the generic flag gate refuses this table on flag 16 (base_paths)."""
    base = tmp_path / "source"
    base.mkdir()
    uri = str(tmp_path / "bronze.lance")
    create_empty(uri, BRONZE, external_base=str(base))
    assert unsupported_features(lance.dataset(uri)) is not None, "flag 16 no longer refused generically; this pin's premise moved"

    assert_creation_contract(uri)  # must not raise
