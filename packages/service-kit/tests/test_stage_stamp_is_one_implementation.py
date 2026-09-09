"""B14: two implementations of the bronze→silver stamp, and nothing compared them.

B14 — "One `transform_batch`, two drivers, one drift pin" (closed 2026-08-23, `f523cd48`). The medallion ships
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
import the service: it is baked into `.docker/ray-cluster.dockerfile`, which installs
`--package ray-cluster-env` (the deps-only platform-environment member, after `packages/ratch` was
dissolved 2026-08-28) — and that member carries `service-kit`, so both images already have this
package.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from service_kit.lakehouse.stage_stamp import LINEAGE_COLUMN, LINEAGE_DATASET_ID_KEY, SOURCE_ROWID_COLUMN, ensure_declared_dataset_id, stamp_stage


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


class TestTheDeclaredIdNamesTHISTier:
    """`lineage.dataset_id` is schema METADATA, and metadata survives every column operation.

    `set_column` / `append_column` / `drop_columns` all preserve it, so a stamp that says nothing about
    it hands the child its PARENT's canonical name — silently, and on both drivers. The rule is the one
    this module already applies to the `lineage` document one line up: the parent's identity describes
    the parent, so leaving it on a child is a false claim, and the honest default is to drop it.

    It is not cosmetic. `maintenance.core.lineage_emit.declared_table_id` reads exactly this key to
    name the dataset a maintenance run is about, so an inherited one files silver's compactions — and
    silver's per-dataset FAIL events, the estate's only per-dataset maintenance failure surface —
    against bronze's node.
    """

    def test_the_stamp_declares_the_destination_it_was_given(self) -> None:
        out = stamp_stage(_rows(), stage="silver", dataset_id="acme$silver")

        assert (out.schema.metadata or {})[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$silver"

    def test_no_declared_id_drops_an_inherited_one(self) -> None:
        """The half that was missing: an unwired driver must not publish its parent's name."""
        bronze = _rows().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"})

        out = stamp_stage(bronze, stage="silver")

        assert LINEAGE_DATASET_ID_KEY.encode() not in (out.schema.metadata or {})

    def test_a_declared_id_REPLACES_an_inherited_one(self) -> None:
        bronze = _rows().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"})

        out = stamp_stage(bronze, stage="silver", dataset_id="acme$silver")

        assert (out.schema.metadata or {})[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$silver"

    def test_other_producers_metadata_survives(self) -> None:
        """Lance keeps other producers' schema metadata (the #21 self-describing coordinates among
        them); a replace would silently destroy it, so this merges."""
        bronze = _rows().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze", "lance.coords": "kept"})

        out = stamp_stage(bronze, stage="silver", dataset_id="acme$silver")

        assert (out.schema.metadata or {})[b"lance.coords"] == b"kept"

    def test_the_empty_schema_a_distributed_lane_creates_its_destination_with_agrees(self) -> None:
        """The distributed lane derives its destination schema from a zero-row slice of the upstream
        (`ray_stage_job._target_schema`), so the metadata rule has to hold with no rows to stamp."""
        bronze = _rows().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"})

        target = stamp_stage(bronze.schema.empty_table(), stage="silver", dataset_id="acme$silver").schema

        assert (target.metadata or {})[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$silver"


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


class TestTheRepairReachesADatasetAMergeWrote:
    """The half a table-level stamp cannot deliver, and the reason it is a separate function.

    Measured on pylance 10.0.0: `merge_insert` carries ROWS and not schema metadata, so a dataset
    created declaring `acme$bronze` still declares `acme$bronze` after a full-sync merge of a table
    declaring `acme$silver`. The cascade's steady-state write IS that merge — deliberately, because an
    overwrite re-mints every `_rowid` and the tier above resolves `source_rowid` against them — so a
    stamp alone would land only on tiers created after it, and every tier already on disk would keep
    its parent's name forever, repaired by no re-run.
    """

    def test_a_merge_alone_does_NOT_move_the_declared_id(self, tmp_path: Path) -> None:
        """The measurement the repair exists for. If this ever starts failing, pylance changed and the
        repair became a no-op rather than a fix — check before deleting it."""
        import lance

        uri = str(tmp_path / "silver.lance")
        rows = pa.table({"id": pa.array([1, 2], pa.int64()), "data": ["a", "b"]})
        lance.write_dataset(rows.replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"}), uri, mode="create")

        lance.dataset(uri).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(
            rows.replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$silver"})
        )

        assert (lance.dataset(uri).schema.metadata or {})[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$bronze"

    def test_the_repair_corrects_it_in_place(self, tmp_path: Path) -> None:
        import lance

        uri = str(tmp_path / "silver.lance")
        rows = pa.table({"id": pa.array([1, 2], pa.int64()), "data": ["a", "b"]})
        lance.write_dataset(rows.replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze", "lance.coords": "kept"}), uri, mode="create")

        assert ensure_declared_dataset_id(uri, "acme$silver") is True

        after = lance.dataset(uri).schema.metadata or {}
        assert after[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$silver"
        assert after[b"lance.coords"] == b"kept", "a metadata-only correction must not drop another producer's keys"

    def test_it_is_IDEMPOTENT_so_every_cascade_tick_may_call_it(self, tmp_path: Path) -> None:
        """A cascade runs this on every write. A second call must commit nothing, or the estate mints a
        version per tick for a value that did not change."""
        import lance

        uri = str(tmp_path / "silver.lance")
        rows = pa.table({"id": pa.array([1], pa.int64()), "data": ["a"]})
        lance.write_dataset(rows, uri, mode="create")

        assert ensure_declared_dataset_id(uri, "acme$silver") is True
        version = lance.dataset(uri).version
        assert ensure_declared_dataset_id(uri, "acme$silver") is False
        assert lance.dataset(uri).version == version, "an unchanged declared id must not mint a version"

    def test_an_UNWIRED_caller_writes_nothing_rather_than_clearing_the_name(self, tmp_path: Path) -> None:
        """Absent means "I was not told", which is not the same claim as "this dataset has no name".
        Clearing here would let one unwired driver erase what a wired one correctly declared."""
        import lance

        uri = str(tmp_path / "silver.lance")
        rows = pa.table({"id": pa.array([1], pa.int64()), "data": ["a"]})
        lance.write_dataset(rows.replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$silver"}), uri, mode="create")

        assert ensure_declared_dataset_id(uri, "") is False
        assert (lance.dataset(uri).schema.metadata or {})[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$silver"
