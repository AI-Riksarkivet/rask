"""Correcting a dataset's declared name must not cost it the rest of its schema metadata.

`ensure_declared_dataset_id` is the primitive a backfill uses to repair the ~20 governed silver/gold
tables that declare their PARENT's id (a cascade tier inherits `lineage.dataset_id` through
`merge_insert`, and the correction only runs on a cascade WRITE, so a tier nothing writes any more
keeps the wrong name forever).

THE HAZARD IS THE WRITE, NOT THE READ. Lance's own spec text calls the operation "Replace schema
metadata", and a replace here would take `lineage.namespace` and `lineage.create_run_id` with it —
the self-describing coordinates a governed table carries — while looking like it worked, because the
key it was asked to fix would be correct afterwards. A backfill that silently strips two keys off
every table it repairs is worse than the stale name it repairs.

So this drives a REAL dataset rather than a double: the question is what pylance does, and no double
can answer it. Measured here on pylance 11.0.0.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pyarrow as pa

from service_kit.lakehouse.stage_stamp import LINEAGE_DATASET_ID_KEY, ensure_declared_dataset_id


def _dataset(tmp: str, metadata: dict[str, str]) -> str:
    import lance

    uri = str(Path(tmp) / "t.lance")
    table = pa.table({"id": pa.array([1, 2, 3])}).replace_schema_metadata(metadata)
    lance.write_dataset(table, uri, mode="create")
    return uri


def test_the_correction_keeps_every_other_metadata_key() -> None:
    """The two `lineage.*` neighbours are the ones that matter — they are how a governed table says
    which namespace it belongs to and which run created it — but an arbitrary user key is asserted
    beside them, because `update_schema_metadata` is not supposed to know which keys are ours."""
    import lance

    with tempfile.TemporaryDirectory() as tmp:
        uri = _dataset(
            tmp,
            {
                LINEAGE_DATASET_ID_KEY: "acme-bronze$events",
                "lineage.namespace": "acme-bronze",
                "lineage.create_run_id": "3ce6d47a-5059-4a50-900f-a649e22aef7b",
                "description": "a table someone described",
            },
        )

        assert ensure_declared_dataset_id(uri, "acme-silver$features") is True

        after = {k.decode(): v.decode() for k, v in (lance.dataset(uri).schema.metadata or {}).items()}
        assert after[LINEAGE_DATASET_ID_KEY] == "acme-silver$features", "the name this exists to fix was not fixed"
        assert after["lineage.namespace"] == "acme-bronze", "the namespace coordinate was destroyed by the correction"
        assert after["lineage.create_run_id"] == "3ce6d47a-5059-4a50-900f-a649e22aef7b", "the creating run was destroyed"
        assert after["description"] == "a table someone described", "a user's own metadata was destroyed"


def test_a_table_already_correct_is_not_rewritten() -> None:
    """Idempotence is what makes the backfill safe to re-run and safe to run on the whole estate: a
    correct table must cost no commit at all, so a second pass is free and creates no version."""
    import lance

    with tempfile.TemporaryDirectory() as tmp:
        uri = _dataset(tmp, {LINEAGE_DATASET_ID_KEY: "acme-silver$features"})
        before = lance.dataset(uri).version

        assert ensure_declared_dataset_id(uri, "acme-silver$features") is False
        assert lance.dataset(uri).version == before, "a no-op correction still committed a new version"


def test_a_table_declaring_NOTHING_gains_the_name() -> None:
    """A dataset written outside the cascade carries no declared id at all. It is not the shape this
    backfill was built for, but it is one it will meet, and the answer is the same: state the name."""
    import lance

    with tempfile.TemporaryDirectory() as tmp:
        uri = _dataset(tmp, {"description": "written by something else"})

        assert ensure_declared_dataset_id(uri, "acme-gold$catalog") is True
        after = {k.decode(): v.decode() for k, v in (lance.dataset(uri).schema.metadata or {}).items()}
        assert after[LINEAGE_DATASET_ID_KEY] == "acme-gold$catalog"
        assert after["description"] == "written by something else"


def test_an_empty_id_is_refused_rather_than_written() -> None:
    """The backfill derives the id from the CATALOG. A lookup that came back empty must not be written
    as the table's name — an empty declared id is worse than a stale one, because the stale one at
    least names a real table and can be recognised as wrong."""
    with tempfile.TemporaryDirectory() as tmp:
        uri = _dataset(tmp, {LINEAGE_DATASET_ID_KEY: "acme-bronze$events"})

        assert ensure_declared_dataset_id(uri, "") is False
