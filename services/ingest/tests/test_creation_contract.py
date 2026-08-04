"""A14 — the creation contract must REFUSE, and the refusal must be provable.

*"Every governed dataset is created with `enable_stable_row_ids=True` and `id` declared as the
unenforced primary key; the catalog's creation contract refuses datasets missing either; a test
proves the refusal."*

The refusal is the whole gate. Both guarantees are creation-time-only — setting
`enable_stable_row_ids` afterwards is a documented silent no-op
(`file_format.md:4011-4013 + guide.md:228-229`) — so a dataset created without them works perfectly, reports
green, and fails months later in someone else's job as a mover that duplicates rows or a
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


BRONZE = pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), pa.field("payload", pa.binary())])


def test_a_correctly_created_dataset_passes(tmp_path: Path) -> None:
    """The contract must accept what the lander itself creates, or it gates nothing but itself."""
    uri = str(tmp_path / "bronze.lance")
    LocalCatalog(BRONZE).ensure_at(uri)

    assert_creation_contract(uri)  # must not raise


def test_a_dataset_created_WITHOUT_stable_row_ids_is_REFUSED(tmp_path: Path) -> None:
    """The flag's absence is invisible at creation and unrepairable afterwards.

    This is the case the gate exists for: nothing about such a dataset looks wrong. It accepts
    writes, reads back correctly, and only betrays itself once compaction moves a row id that a
    silver `source_rowid` was pointing at.
    """
    uri = str(tmp_path / "no-ids.lance")
    lance.write_dataset(BRONZE.empty_table(), uri, mode="create", data_storage_version="2.2")

    with pytest.raises(CreationContractError, match="enable_stable_row_ids"):
        assert_creation_contract(uri)


def test_a_dataset_with_no_id_COLUMN_is_REFUSED(tmp_path: Path) -> None:
    """No `id` means `merge_insert` has nothing to converge on.

    A hop against such a dataset appends where it should update, so E2's "a redelivered event
    converges" claim silently becomes "a redelivered event duplicates" — and redelivery is normal, not
    exceptional, on an at-least-once bus.
    """
    uri = str(tmp_path / "no-id-col.lance")
    schema = pa.schema([pa.field("source_uri", pa.string())])
    lance.write_dataset(schema.empty_table(), uri, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)

    with pytest.raises(CreationContractError, match="`id` column"):
        assert_creation_contract(uri)


def test_the_refusal_SAYS_WHY_and_says_it_is_unrepairable(tmp_path: Path) -> None:
    """An operator who cannot act on a refusal will disable the gate.

    The message has to carry the consequence ("ids that move under compaction") and the fact that
    fixing it in place is impossible — otherwise the obvious reaction is to set the flag and re-run,
    which succeeds and changes nothing.
    """
    uri = str(tmp_path / "no-ids.lance")
    lance.write_dataset(BRONZE.empty_table(), uri, mode="create", data_storage_version="2.2")

    with pytest.raises(CreationContractError) as raised:
        assert_creation_contract(uri)

    message = str(raised.value)
    assert "compaction" in message
    assert "silent no-op" in message
    assert "A14" in message


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


def test_stable_row_ids_are_what_the_delta_read_depends_on(tmp_path: Path) -> None:
    """Ties the gate to the thing it protects, so the reason cannot drift from the rule.

    The contract is not about a flag; it is about `_row_created_at_version` being answerable. This
    asserts the capability directly, so if lance ever decouples the two, the gate's justification is
    what fails rather than staying quietly stale.
    """
    uri = str(tmp_path / "bronze.lance")
    LocalCatalog(BRONZE).ensure_at(uri)
    lance.dataset(uri).insert(pa.table({"id": [1], "source_uri": ["file:///1"], "payload": [b"x"]}, schema=BRONZE))

    rows = lance.dataset(uri).to_table(with_row_id=True, filter="_row_created_at_version > 1")

    assert rows.num_rows == 1
    assert "_rowid" in rows.column_names
