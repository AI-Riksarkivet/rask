"""The dummy runner against REAL Lance — A19 delta correctness, E2 idempotency, D2 no-copy.

These are the mechanics the whole cascade rests on, exercised without a GPU or a model. That is the
runner's reason to exist: A11 has to be runnable on a laptop and in CI, and an end-to-end test nobody
can run is indistinguishable from a broken one.

Sealed project — invisible to the root pytest (runners/* match no workspace glob, and testpaths does
not list them). Run: `uv run --project runners/dummy pytest runners/dummy/tests`.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest

from dummy_runner.job import read_delta, run, write_silver
from dummy_runner.transform import EMBED_DIM, SILVER_SCHEMA, transform_batch

BRONZE = pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), pa.field("payload", pa.binary())])


def _bronze(tmp: Path, ids: list[int]) -> str:
    uri = str(tmp / "bronze.lance")
    table = pa.table(
        {
            "id": ids,
            "source_uri": [f"file:///p/{i}" for i in ids],
            "payload": [f"page {i} body text".encode() for i in ids],
        },
        schema=BRONZE,
    )
    lance.write_dataset(table, uri, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    return uri


def _append(uri: str, ids: list[int]) -> None:
    lance.dataset(uri).insert(
        pa.table(
            {
                "id": ids,
                "source_uri": [f"file:///p/{i}" for i in ids],
                "payload": [f"page {i} body text".encode() for i in ids],
            },
            schema=BRONZE,
        )
    )


def test_the_transform_is_deterministic() -> None:
    """A19's replay clause: the same bronze must produce byte-identical silver.

    A random embedding would make an E5 replay produce a different silver from the same input, and
    "replay converges to identical content hashes" could never hold.
    """
    batch = pa.table({"id": [1], "source_uri": ["u"], "payload": [b"a b c"]}, schema=BRONZE)
    first = transform_batch(batch)
    second = transform_batch(batch)
    assert first.equals(second)
    assert first.column("word_count")[0].as_py() == 3
    assert len(first.column("embedding")[0].as_py()) == EMBED_DIM


def test_silver_references_bronze_rather_than_copying_bytes() -> None:
    """D2: silver stores id + source_rowid; the payload stays in bronze.

    Carrying blobs forward is the measured defect in today's cascade — silver and gold each hold a
    complete byte copy. A schema that cannot express the copy cannot regress into it.
    """
    assert "payload" not in [f.name for f in SILVER_SCHEMA]
    assert "source_rowid" in [f.name for f in SILVER_SCHEMA]


def test_a19_the_delta_read_returns_exactly_the_new_rows(tmp_path: Path) -> None:
    """Two successive publishes of N then M rows -> exactly N, then exactly M. No overlap, no misses."""
    uri = _bronze(tmp_path, [1, 2, 3])
    v1 = lance.dataset(uri).version

    _append(uri, [4, 5])
    delta = read_delta(uri, v1)
    assert sorted(delta.column("id").to_pylist()) == [4, 5]

    v2 = lance.dataset(uri).version
    _append(uri, [6])
    assert sorted(read_delta(uri, v2).column("id").to_pylist()) == [6]

    # And the full read is still available when there is no boundary — a first run has no delta.
    assert len(read_delta(uri, None).column("id").to_pylist()) == 6


def test_e2_a_redelivered_hop_converges_instead_of_duplicating(tmp_path: Path) -> None:
    """merge_insert on the stable id: running the same hop twice leaves one row per id."""
    uri = _bronze(tmp_path, [1, 2, 3])
    silver_uri = str(tmp_path / "silver.lance")
    rows = transform_batch(read_delta(uri, None))

    first = write_silver(silver_uri, rows, run_id="r1")
    second = write_silver(silver_uri, rows, run_id="r1")

    assert first["rows"] == 3
    assert second["rows"] == 3, "a redelivered hop duplicated rows — merge_insert is not converging"


def test_an_empty_delta_is_a_no_op_not_an_empty_version(tmp_path: Path) -> None:
    """A redelivered event whose rows were already processed must not fire a publication.

    Writing an empty version would emit an arrival event for data nobody added, and every downstream
    mover would wake for nothing.
    """
    uri = _bronze(tmp_path, [1])
    latest = lance.dataset(uri).version
    out = run({"FROM_URI": uri, "TO_URI": str(tmp_path / "s.lance"), "BASE_VERSION": str(latest)})

    assert out["skipped"] is True
    assert out["version"] is None
    assert not Path(tmp_path / "s.lance").exists(), "an empty delta must not create a dataset"


def test_the_job_runs_end_to_end_from_env(tmp_path: Path) -> None:
    """The whole hop as the Ray job runs it — env in, silver out."""
    uri = _bronze(tmp_path, [1, 2, 3, 4])
    out = run({"FROM_URI": uri, "TO_URI": str(tmp_path / "silver.lance"), "RUN_ID": "run-xyz"})

    assert out["rows_in"] == 4
    assert out["rows_written"] == 4
    assert out["run_id"] == "run-xyz"
    silver = lance.dataset(str(tmp_path / "silver.lance")).to_table()
    assert sorted(silver.column("id").to_pylist()) == [1, 2, 3, 4]
    assert silver.column("checksum")[0].as_py()


def test_missing_env_refuses_loudly() -> None:
    with pytest.raises(ValueError, match="FROM_URI and TO_URI"):
        run({})
