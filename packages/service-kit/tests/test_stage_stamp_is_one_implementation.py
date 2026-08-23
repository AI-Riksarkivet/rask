"""B14: two implementations of the bronze→silver stamp, and nothing compared them.

`open_batch_process.md` B14 — "One `transform_batch`, two drivers, one drift pin." The medallion ships
the stage transform twice: `medallion/services/compute.py` runs it in-process, and
`scripts/ray_stage_job.py` runs it on the cluster. The Ray copy's own docstring admits the arrangement
— "Mirrors compute._carry_source_rowid + _stamp_stage" — and a mirror maintained by hand is a mirror
that drifts.

IT HAD ALREADY DRIFTED, and the divergence is invisible to every existing test. Given one table
carrying a `stage` column, the two produce DIFFERENT SCHEMAS:

    medallion: ['id', 'stage', 'data', 'source_rowid']     set_column, in place
    ray:       ['id', 'data', 'source_rowid', 'stage']     drop_columns + append, at the end

Column ORDER is not cosmetic here. `lance.write_dataset(mode="overwrite")` takes the table's schema as
the dataset's schema, so a silver table's column order depends on WHICH COMPUTE PATH WROTE IT. Two runs
of the same lane over the same data — one in-process, one on Ray — leave datasets whose schemas are not
equal, and any consumer comparing schemas, reading positionally, or diffing a manifest sees a change
that no data change caused.

The fix is the one `references/anti-patterns.md` § "Mixed I/O and business logic" prescribes: the
stamping is pure — a table in, a table out, no storage, no Ray — so it is extracted here and both
drivers import it. This module is the right home rather than the medallion because the Ray job CANNOT
import the service: it is baked into `.docker/ray-cluster.dockerfile`, which installs `--package ratch`
— and ratch depends on `service-kit[lancekit]`, so both images already carry this package.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from service_kit.lakehouse.stage_stamp import LINEAGE_COLUMN, SOURCE_ROWID_COLUMN, stamp_stage


def _rows(**extra: object) -> pa.Table:
    base: dict[str, object] = {"id": [1, 2], "data": ["a", "b"]}
    base.update(extra)
    return pa.table(base)


class TestTheStampIsPure:
    """No storage, no Ray, no lance — which is what lets one implementation serve both drivers."""

    def test_it_takes_a_table_and_returns_a_table(self) -> None:
        out = stamp_stage(_rows(), stage="silver")
        assert isinstance(out, pa.Table)

    def test_it_does_not_mutate_its_input(self) -> None:
        table = _rows()
        before = table.column_names[:]
        stamp_stage(table, stage="silver")
        assert table.column_names == before


class TestColumnOrderIsStable:
    """The drift that was live: a re-stamp must not move the column."""

    def test_an_existing_stage_column_keeps_its_position(self) -> None:
        table = pa.table({"id": [1], "stage": ["bronze"], "data": ["a"]})

        out = stamp_stage(table, stage="silver")

        assert out.column_names == ["id", "stage", "data"], "re-stamping moved the column, so a dataset's schema depends on which compute path wrote it"
        assert out.column("stage").to_pylist() == ["silver"]

    def test_a_missing_stage_column_is_appended(self) -> None:
        out = stamp_stage(_rows(), stage="silver")
        assert out.column_names == ["id", "data", "stage"]

    def test_stamping_twice_is_idempotent_in_shape(self) -> None:
        """The cascade is overwrite-only and re-runs, so a second pass must not keep reshaping."""
        once = stamp_stage(_rows(), stage="silver")
        twice = stamp_stage(once, stage="silver")
        assert once.schema.equals(twice.schema)


class TestRootProvenance:
    def test_the_head_mints_source_rowid_from_the_reserved_metacolumn(self) -> None:
        table = _rows(_rowid=pa.array([7, 8], pa.uint64()))

        out = stamp_stage(table, stage="silver")

        assert out.column(SOURCE_ROWID_COLUMN).to_pylist() == [7, 8]

    def test_rowid_is_never_persisted(self) -> None:
        """A reserved name that advances on the next overwrite — persisting it records a lie."""
        out = stamp_stage(_rows(_rowid=pa.array([7, 8], pa.uint64())), stage="silver")
        assert "_rowid" not in out.column_names

    def test_a_later_stage_keeps_the_ROOT_id_rather_than_re_minting(self) -> None:
        """source_rowid names the BRONZE row a gold row descends from. Re-minting from the immediate
        parent would silently reroot the chain one tier down."""
        table = _rows(source_rowid=pa.array([99, 100], pa.uint64()), _rowid=pa.array([1, 2], pa.uint64()))

        out = stamp_stage(table, stage="gold")

        assert out.column(SOURCE_ROWID_COLUMN).to_pylist() == [99, 100]
        assert "_rowid" not in out.column_names

    def test_no_rowid_and_no_source_rowid_is_not_an_error(self) -> None:
        """A tabular upstream read without with_row_id has neither. It must still stamp."""
        out = stamp_stage(_rows(), stage="silver")
        assert SOURCE_ROWID_COLUMN not in out.column_names


class TestTheLineageColumn:
    def test_a_document_is_stamped_as_json(self) -> None:
        out = stamp_stage(_rows(), stage="silver", lineage='{"run":"r1"}')
        assert LINEAGE_COLUMN in out.column_names
        assert out.column(LINEAGE_COLUMN).to_pylist() == ['{"run":"r1"}', '{"run":"r1"}']

    def test_an_inherited_document_is_REPLACED_not_appended_twice(self) -> None:
        """The re-stamp exists so a gold row does not claim its parent's provenance."""
        table = _rows(**{LINEAGE_COLUMN: ['{"run":"parent"}', '{"run":"parent"}']})

        out = stamp_stage(table, stage="gold", lineage='{"run":"child"}')

        assert out.column(LINEAGE_COLUMN).to_pylist() == ['{"run":"child"}', '{"run":"child"}']
        assert out.column_names.count(LINEAGE_COLUMN) == 1

    def test_no_document_drops_an_inherited_one(self) -> None:
        """Carrying the parent's document forward unchanged would be a false claim about this row."""
        table = _rows(**{LINEAGE_COLUMN: ['{"run":"parent"}', '{"run":"parent"}']})

        out = stamp_stage(table, stage="gold", lineage="")

        assert LINEAGE_COLUMN not in out.column_names


class TestBothDriversUseIt:
    """The pin. An extracted function nobody calls leaves the two copies exactly as they were."""

    @pytest.mark.parametrize(
        "path",
        [
            "services/medallion/src/medallion/services/compute.py",
            "scripts/ray_stage_job.py",
        ],
    )
    def test_the_driver_imports_the_shared_stamp(self, path: str) -> None:
        from pathlib import Path

        source = (Path(__file__).resolve().parents[3] / path).read_text()
        assert "stage_stamp" in source, f"{path} still carries its own copy of the stamp"
