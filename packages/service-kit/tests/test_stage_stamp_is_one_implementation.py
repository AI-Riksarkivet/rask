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

from service_kit.lakehouse.stage_stamp import LINEAGE_COLUMN, LINEAGE_DATASET_ID_KEY, SOURCE_ROWID_COLUMN, ensure_declared_dataset_id, stamp_stage


def _rows(**extra: object) -> pa.Table:
    base: dict[str, object] = {"id": [1, 2], "data": ["a", "b"]}
    base.update(extra)
    return pa.table(base)


class TestColumnOrderIsStable:
    """The drift that was live: a re-stamp must not move the column."""

    def test_an_existing_stage_column_keeps_its_position(self) -> None:
        table = pa.table({"id": [1], "stage": ["bronze"], "data": ["a"]})

        out = stamp_stage(table, stage="silver", stable_row_ids=True)

        assert out.column_names == ["id", "stage", "data"], "re-stamping moved the column, so a dataset's schema depends on which compute path wrote it"
        assert out.column("stage").to_pylist() == ["silver"]

    def test_a_missing_stage_column_is_appended(self) -> None:
        out = stamp_stage(_rows(), stage="silver", stable_row_ids=True)
        assert out.column_names == ["id", "data", "stage"]


class TestRootProvenance:
    def test_no_rowid_and_no_source_rowid_is_not_an_error(self) -> None:
        """A tabular upstream read without with_row_id has neither. It must still stamp."""
        out = stamp_stage(_rows(), stage="silver", stable_row_ids=True)
        assert SOURCE_ROWID_COLUMN not in out.column_names


class TestTheLineageColumn:
    def test_an_inherited_document_is_REPLACED_not_appended_twice(self) -> None:
        """The re-stamp exists so a gold row does not claim its parent's provenance."""
        table = _rows(**{LINEAGE_COLUMN: ['{"run":"parent"}', '{"run":"parent"}']})

        out = stamp_stage(table, stage="gold", stable_row_ids=True, lineage='{"run":"child"}')

        assert out.column(LINEAGE_COLUMN).to_pylist() == ['{"run":"child"}', '{"run":"child"}']
        assert out.column_names.count(LINEAGE_COLUMN) == 1

    def test_no_document_drops_an_inherited_one(self) -> None:
        """Carrying the parent's document forward unchanged would be a false claim about this row."""
        table = _rows(**{LINEAGE_COLUMN: ['{"run":"parent"}', '{"run":"parent"}']})

        out = stamp_stage(table, stage="gold", stable_row_ids=True, lineage="")

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

    def test_no_declared_id_drops_an_inherited_one(self) -> None:
        """The half that was missing: an unwired driver must not publish its parent's name."""
        bronze = _rows().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"})

        out = stamp_stage(bronze, stage="silver", stable_row_ids=True)

        assert LINEAGE_DATASET_ID_KEY.encode() not in (out.schema.metadata or {})

    def test_a_declared_id_REPLACES_an_inherited_one(self) -> None:
        bronze = _rows().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"})

        out = stamp_stage(bronze, stage="silver", stable_row_ids=True, dataset_id="acme$silver")

        assert (out.schema.metadata or {})[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$silver"

    def test_the_empty_schema_a_distributed_lane_creates_its_destination_with_agrees(self) -> None:
        """The distributed lane derives its destination schema from a zero-row slice of the upstream
        (`ray_stage_job._target_schema`), so the metadata rule has to hold with no rows to stamp."""
        bronze = _rows().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"})

        target = stamp_stage(bronze.schema.empty_table(), stage="silver", stable_row_ids=True, dataset_id="acme$silver").schema

        assert (target.metadata or {})[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$silver"


class TestTheRepairReachesADatasetAMergeWrote:
    """The half a table-level stamp cannot deliver, and the reason it is a separate function.

    Measured on pylance 10.0.0: `merge_insert` carries ROWS and not schema metadata, so a dataset
    created declaring `acme$bronze` still declares `acme$bronze` after a full-sync merge of a table
    declaring `acme$silver`. The cascade's steady-state write IS that merge — deliberately, because an
    overwrite re-mints every `_rowid` and the tier above resolves `source_rowid` against them — so a
    stamp alone would land only on tiers created after it, and every tier already on disk would keep
    its parent's name forever, repaired by no re-run.
    """

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

    def test_the_correction_keeps_every_other_metadata_key(self, tmp_path: Path) -> None:
        """Correcting the declared name must not cost the dataset the rest of its schema metadata.

        Lance's spec calls the operation "Replace schema metadata", and a replace here would take
        `lineage.namespace` and `lineage.create_run_id` with it, the self-describing coordinates a
        governed table carries, while looking like it worked. An arbitrary user key is asserted beside
        them, because `update_schema_metadata` is not supposed to know which keys are ours. Driven on a
        real dataset: the question is what pylance does, and no double can answer it.
        """
        import lance

        uri = str(tmp_path / "t.lance")
        metadata = {
            LINEAGE_DATASET_ID_KEY: "acme-bronze$events",
            "lineage.namespace": "acme-bronze",
            "lineage.create_run_id": "3ce6d47a-5059-4a50-900f-a649e22aef7b",
            "description": "a table someone described",
        }
        lance.write_dataset(pa.table({"id": pa.array([1, 2, 3])}).replace_schema_metadata(metadata), uri, mode="create")

        assert ensure_declared_dataset_id(uri, "acme-silver$features") is True

        after = {k.decode(): v.decode() for k, v in (lance.dataset(uri).schema.metadata or {}).items()}
        assert after[LINEAGE_DATASET_ID_KEY] == "acme-silver$features", "the name this exists to fix was not fixed"
        assert after["lineage.namespace"] == "acme-bronze", "the namespace coordinate was destroyed by the correction"
        assert after["lineage.create_run_id"] == "3ce6d47a-5059-4a50-900f-a649e22aef7b", "the creating run was destroyed"
        assert after["description"] == "a table someone described", "a user's own metadata was destroyed"
