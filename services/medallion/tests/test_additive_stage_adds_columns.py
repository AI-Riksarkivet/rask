"""An additive re-run adds columns; it does not rewrite the tier — §8 change 4.

`_index_lineage`'s docstring states the cost this removes, and states it as a fact about the write
mode rather than a preference: the JSON index "must be (re)built after every stage write because the
cascade writes `mode="overwrite"`, which drops the dataset's indices". An overwrite also rewrites
every carried column to produce bytes byte-identical to the ones already on disk.

`add_columns` appends new data files per fragment and leaves the existing ones untouched (measured
separately by `scripts/measure_add_columns_on_blob_table.py`), so an additive run pays for the new
columns only and the indices survive.

**THE DANGER IS POSITIONAL ALIGNMENT, and most of this file is about that.** `add_columns` matches
the incoming reader to existing fragments BY POSITION. Attaching derived values to a tier whose rows
have shifted would misfile every one of them, in a governed dataset, with nothing raised. So the
guard compares `source_rowid` element-wise — the row identity the estate already mints — and anything
short of an exact match falls back to the overwrite that was always correct.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa

from medallion.services.compute import transform_stage


def _upstream(uri: str, ids: list[int], note: str = "a") -> None:
    lance.write_dataset(
        pa.table({"id": pa.array(ids, pa.int64()), "note": pa.array([note] * len(ids), pa.string())}),
        uri,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )


def _data_files(uri: str) -> set[str]:
    """Every data file the dataset currently references, by name."""
    return {f.path for frag in lance.dataset(uri).get_fragments() for f in frag.data_files()}


class TestARedeliveredTriggerWritesNothing:
    """The most common path, and the one that used to cost the most.

    Dapr delivers at least once, so a stage runner re-runs a stage it has already completed as a matter of
    routine. That re-run produced exactly the columns already on disk — and then rewrote the entire
    tier to put them there again, and dropped the JSON index on the way so it had to be rebuilt.
    """

    def test_an_identical_rerun_adds_no_columns_and_rewrites_nothing(self, tmp_path: Path) -> None:
        bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
        _upstream(bronze, [1, 2, 3])
        transform_stage(bronze, silver, {}, stage="silver")
        files_after_first = _data_files(silver)
        version_after_first = lance.dataset(silver).version

        transform_stage(bronze, silver, {}, stage="silver")

        assert _data_files(silver) == files_after_first, "a redelivered trigger rewrote the tier"
        assert lance.dataset(silver).version == version_after_first, "a redelivered trigger committed a new version for no change"


class TestTheGuardRefusesEveryMisalignedShape:
    """Each of these WOULD misfile derived values if `add_columns` were used. All must fall back."""

    def test_a_changed_upstream_VALUE_at_the_same_count_falls_back(self, tmp_path: Path) -> None:
        """The case row-count alone would miss, and the reason the guard compares identity.

        Same number of rows, different content. An addition would leave the carried `note` column
        holding the OLD values while any new column described the new ones — a row whose columns
        disagree about which upstream row it is.
        """
        bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
        _upstream(bronze, [1, 2, 3], note="old")
        transform_stage(bronze, silver, {}, stage="silver")
        assert set(lance.dataset(silver).to_table().column("note").to_pylist()) == {"old"}

        _upstream(bronze, [7, 8, 9], note="new")
        transform_stage(bronze, silver, {}, stage="silver")

        out = lance.dataset(silver).to_table()
        assert set(out.column("note").to_pylist()) == {"new"}, "the carried column kept its stale values — the guard accepted a misaligned addition"
        assert set(out.column("id").to_pylist()) == {7, 8, 9}
