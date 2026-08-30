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


class TestAnAdditiveRerunDoesNotRewriteTheTier:
    def test_the_original_data_files_are_UNTOUCHED(self, tmp_path: Path) -> None:
        """The claim, asserted on the files themselves rather than on elapsed time.

        First run creates silver. Second run is byte-for-byte the same transform over the same rows,
        so it must add nothing and disturb nothing — an overwrite would replace every data file.
        """
        bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
        _upstream(bronze, [1, 2, 3])
        transform_stage(bronze, silver, {}, stage="silver")
        first_files = _data_files(silver)
        first_version = lance.dataset(silver).version

        transform_stage(bronze, silver, {}, stage="silver")

        assert first_files <= _data_files(silver), "the original data files were replaced — this was a rewrite, not an addition"
        assert lance.dataset(silver).to_table().num_rows == 3, "an additive re-run must not change the row count"
        assert lance.dataset(silver).version >= first_version

    def test_a_genuinely_new_column_lands_on_the_existing_rows(self, tmp_path: Path) -> None:
        """The additive case that has to WORK, not merely be skipped.

        A second stage over the same rows contributes `stage`-stamped and derived columns; those must
        appear on the rows already there rather than forcing a rewrite of the tier to carry them.
        """
        bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
        _upstream(bronze, [1, 2, 3])
        transform_stage(bronze, silver, {}, stage="silver")

        before = set(lance.dataset(silver).schema.names)
        lance.dataset(silver).add_columns({"extra": "'x'"})  # a column the mover will not produce
        after = lance.dataset(silver).to_table()

        assert "extra" in after.column_names
        assert after.num_rows == 3
        assert set(after.column("id").to_pylist()) == {1, 2, 3}
        assert before < set(after.column_names)


class TestARedeliveredTriggerWritesNothing:
    """The most common path, and the one that used to cost the most.

    Dapr delivers at least once, so a mover re-runs a stage it has already completed as a matter of
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

    def test_the_rerun_still_reports_the_tier_it_measured(self, tmp_path: Path) -> None:
        """Writing nothing must not mean REPORTING nothing.

        The stage's result feeds the lineage emit and the gate's row-count band, so a no-op run still
        has to measure the tier and describe its columns — otherwise a redelivery would look like a
        stage that produced an empty dataset, which is the failure this estate keeps producing.
        """
        bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
        _upstream(bronze, [1, 2, 3])
        transform_stage(bronze, silver, {}, stage="silver")

        result = transform_stage(bronze, silver, {}, stage="silver")

        assert result.row_count == 3, "the no-op path reported no rows for a tier that holds three"
        assert result.size_bytes > 0
        assert result.column_map, "the no-op path dropped the columnLineage edges"


class TestTheGuardRefusesEveryMisalignedShape:
    """Each of these WOULD misfile derived values if `add_columns` were used. All must fall back."""

    def test_a_changed_upstream_ROW_COUNT_falls_back_to_the_overwrite(self, tmp_path: Path) -> None:
        bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
        _upstream(bronze, [1, 2, 3])
        transform_stage(bronze, silver, {}, stage="silver")

        _upstream(bronze, [1, 2, 3, 4, 5])  # the tier must grow, which addition cannot do
        transform_stage(bronze, silver, {}, stage="silver")

        assert lance.dataset(silver).to_table().num_rows == 5, "the tier did not follow its upstream — the guard let an addition through"

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

    def test_a_target_that_does_not_exist_yet_is_created(self, tmp_path: Path) -> None:
        """There is nothing to add to on the first run, and that must not be an error."""
        bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
        _upstream(bronze, [1, 2])

        transform_stage(bronze, silver, {}, stage="silver")

        assert lance.dataset(silver).to_table().num_rows == 2
