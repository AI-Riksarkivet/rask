"""The gate's decision over REAL Lance datasets — the composition neither existing suite covers.

`test_gate_decision.py` drives `gate_decision` with synthetic lists of assertion names, so it pins the
ORDERING and nothing about what produces those names. The seam it leaves is a dataset that exists ON
DISK, read back at a pinned version by the POST-commit `assert_quality`, and the outcome the gate
actually returns for it. That is the seam the live cascade runs.

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
