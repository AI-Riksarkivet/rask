"""The gate's decision over REAL Lance datasets — the composition neither existing suite covers.

Two suites already exist and each tests one half. `test_gate_decision.py` drives `gate_decision` with
synthetic lists of assertion names, so it pins the ORDERING and nothing about what produces those
names. `tests/unit/test_dummy_quality_gate.py` drives `assert_quality_on_batch` — the PRE-COMMIT form,
over an in-memory Arrow table — and asserts on `passed()`, never on the gate.

So the seam between them was untested: a dataset that exists ON DISK, read back at a pinned version by
the POST-commit `assert_quality`, and the outcome the gate actually returns for it. That is the seam
the live cascade runs, and the one a reader would assume was covered because both halves are.

Configured as the live estate is (verified on rask-bronze-to-silver 2026-08-23):
MEDALLION_QUALITY_KEY_COLUMN=id, MEDALLION_REQUIRED_COLUMNS=id, MEDALLION_CASCADE_VIA_PUBLISH=true.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from medallion.services.gate_decision import GateOutcome, gate_decision
from service_kit.lakehouse.quality import assert_quality


KEY_COLUMN = "id"
REQUIRED = ("id",)


def _write(tmp_path, ids: list[int | None], *, drop_id: bool = False, name: str = "silver") -> str:
    """A real Lance dataset on disk, in the producer's own bronze/silver shape (compute.py::seed_bronze)."""
    lance = pytest.importorskip("lance")
    n = len(ids)
    columns: dict[str, pa.Array] = {
        "payload": pa.array([f"event-{i}" for i in range(n)]),
        "stage": pa.array(["silver"] * n, pa.string()),
    }
    if not drop_id:
        columns = {"id": pa.array(ids, pa.int64()), **columns}
    uri = str(tmp_path / f"{name}.lance")
    lance.write_dataset(pa.table(columns), uri, mode="overwrite", data_storage_version="2.2")
    return uri


def _decide(uri: str, *, band_reasons: tuple[str, ...] = ()) -> tuple[GateOutcome, list[str]]:
    """assert_quality -> the names that failed -> gate_decision, exactly as a stage composes them."""
    assertions = assert_quality(uri, {}, key_column=KEY_COLUMN, required_columns=REQUIRED)
    failed = [a.assertion for a in assertions if not a.success]
    outcome = gate_decision(
        failed_assertions=failed,
        band_reasons=band_reasons,
        has_target=True,
        has_catalog=True,
        has_pub_topic=True,
    )
    return outcome, failed


def test_a_clean_dataset_publishes(tmp_path) -> None:
    outcome, failed = _decide(_write(tmp_path, [0, 1, 2]))
    assert failed == []
    assert outcome is GateOutcome.PUBLISH


def test_a_NULL_key_blocks(tmp_path) -> None:
    """A null identity is a broken join, and no approval makes it right — hence BLOCK, not HOLD.

    It also breaks the merge_insert the next hop performs: merging on a null key matches nothing and
    silently appends, so the corruption compounds rather than surfacing.
    """
    outcome, failed = _decide(_write(tmp_path, [0, 1, None, 3]))
    assert "not_null" in failed
    assert outcome is GateOutcome.BLOCK


def test_an_EMPTY_dataset_blocks(tmp_path) -> None:
    """An empty promotion is the silent failure: every downstream read succeeds and returns nothing."""
    outcome, failed = _decide(_write(tmp_path, []))
    assert "row_count_positive" in failed
    assert outcome is GateOutcome.BLOCK


def test_dropping_a_DECLARED_column_blocks(tmp_path) -> None:
    """Schema-on-write stays free; only PROMOTING a version that dropped a declared column is stopped."""
    outcome, failed = _decide(_write(tmp_path, [0, 1, 2], drop_id=True))
    assert "column_declared" in failed
    assert outcome is GateOutcome.BLOCK


def test_not_null_SKIPS_rather_than_fails_when_the_key_is_absent(tmp_path) -> None:
    """Different stages may key differently, so an absent key column is not a null key.

    Worth pinning next to the test above: dropping `id` fails `column_declared` because it was
    DECLARED, not because `not_null` fired. If the two were ever conflated, a stage that legitimately
    keys on something else would start blocking.
    """
    assertions = assert_quality(_write(tmp_path, [0, 1], drop_id=True), {}, key_column=KEY_COLUMN, required_columns=())
    assert [a.assertion for a in assertions if not a.success] == []
    assert "not_null" not in [a.assertion for a in assertions]


def test_a_BLOCK_outranks_a_band_HOLD_on_real_data(tmp_path) -> None:
    """The ordering that matters most, over a dataset that genuinely fails both tests.

    test_gate_decision.py already pins this with synthetic names. Repeating it here is not duplication:
    it proves the composition delivers a non-empty `failed_assertions` to the gate, which is the part a
    synthetic test assumes. A corrupt batch parked on an approval nobody should ever be offered is the
    failure this ordering exists to prevent.
    """
    outcome, failed = _decide(_write(tmp_path, [0, None, 2]), band_reasons=("row count fell 40%",))
    assert failed, "the composition handed the gate no failed assertions"
    assert outcome is GateOutcome.BLOCK


def test_a_band_breach_alone_HOLDS(tmp_path) -> None:
    """A clean dataset that merely moved a lot is a question for a person, not a verdict."""
    outcome, failed = _decide(_write(tmp_path, [0, 1, 2]), band_reasons=("row count fell 40%",))
    assert failed == []
    assert outcome is GateOutcome.HOLD


def test_the_gate_reads_the_PINNED_version_not_the_latest(tmp_path) -> None:
    """The publish gate tags a specific version while another writer may have committed since.

    It once passed only the uri, so the pin it had just taken was discarded and the assertions ran
    against whatever was latest — and the silent direction is publishing a DIRTY version because a
    later clean one exists. This drives exactly that: v1 is dirty, v2 is clean.
    """
    lance = pytest.importorskip("lance")
    uri = _write(tmp_path, [0, None, 2])
    dirty_version = lance.dataset(uri).version
    lance.write_dataset(
        pa.table({"id": pa.array([0, 1, 2], pa.int64()), "payload": pa.array(["a", "b", "c"]), "stage": pa.array(["silver"] * 3)}),
        uri,
        mode="overwrite",
        data_storage_version="2.2",
    )

    latest = assert_quality(uri, {}, key_column=KEY_COLUMN, required_columns=REQUIRED)
    assert [a.assertion for a in latest if not a.success] == [], "the later version is clean"

    pinned = assert_quality(uri, {}, key_column=KEY_COLUMN, required_columns=REQUIRED, version=dirty_version)
    assert "not_null" in [a.assertion for a in pinned if not a.success], "the pin was discarded — a dirty version would publish"
