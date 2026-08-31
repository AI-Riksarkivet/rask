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
    # `_rowid` is supplied because the transform now REFUSES to invent one — see the sibling test
    # below. Real bronze always carries it: the cascade creates every dataset with stable row ids.
    batch = pa.table({"id": [1], "source_uri": ["u"], "payload": [b"a b c"]}, schema=BRONZE).append_column("_rowid", pa.array([77], pa.uint64()))
    first = transform_batch(batch)
    second = transform_batch(batch)
    assert first.equals(second)
    assert first.column("word_count")[0].as_py() == 3
    assert len(first.column("embedding")[0].as_py()) == EMBED_DIM


def test_the_transform_REFUSES_to_invent_provenance() -> None:
    """Owner ruling D1: a fabricated parent id is worse than a missing one, because it is queryable.

    This read `else list(range(len(ids)))` — the row's POSITION written into the column that means
    "the bronze row this descends from". Asked which rows descend from a corrupted document, it
    answered with whichever rows sat at those offsets: confident, and wrong.
    """
    batch = pa.table({"id": [1], "source_uri": ["u"], "payload": [b"a b c"]}, schema=BRONZE)

    with pytest.raises(ValueError, match="provenance"):
        transform_batch(batch)


def test_the_silver_schema_carries_every_governed_column() -> None:
    """What the catalog's publish door refuses a tier for not having.

    Pinned here as well as at the door because this runner cannot import the platform's definition —
    it is sealed — so the two must agree by test rather than by shared code.
    """
    batch = pa.table({"id": [1], "source_uri": ["u"], "payload": [b"a b c"]}, schema=BRONZE).append_column("_rowid", pa.array([77], pa.uint64()))
    out = transform_batch(batch, stage="silver", lineage='{"run_id":"r1"}')

    assert {"stage", "lineage", "source_rowid"} <= set(out.schema.names)
    assert pa.types.is_uint64(out.schema.field("source_rowid").type), "uint64 is the width Lance's stable row id uses"
    assert out.column("source_rowid").to_pylist() == [77], "the REAL parent, not the row's position"


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


def test_a19_the_delta_is_asserted_at_ROW_ID_level_not_just_by_count(tmp_path: Path) -> None:
    """A19 in full: the two hops' CDF row ids must not overlap and must miss nothing.

    Counting is not enough. A delta predicate that was off by one version, or that silently rescanned
    the tier and happened to return the right NUMBER of rows, passes a count assertion while feeding
    the mover the wrong rows entirely. The stable row id is the identity that makes the claim
    checkable, which is also why the creation flag that produces it is gated (A14).
    """
    uri = _bronze(tmp_path, [1, 2, 3])
    v1 = lance.dataset(uri).version

    _append(uri, [4, 5])
    first = read_delta(uri, v1)
    v2 = lance.dataset(uri).version

    _append(uri, [6, 7])
    second = read_delta(uri, v2)

    first_ids = set(first.column("_rowid").to_pylist())
    second_ids = set(second.column("_rowid").to_pylist())

    assert first_ids & second_ids == set(), "the two hops overlap — rows would be transformed twice"

    everything = set(read_delta(uri, None).column("_rowid").to_pylist())
    original = everything - first_ids - second_ids
    assert len(original) == 3, "the pre-boundary rows were re-delivered into a delta"
    assert first_ids | second_ids | original == everything, "a row belongs to no hop — it would never be transformed"


def test_a19_a_full_E5_replay_converges_to_identical_CONTENT(tmp_path: Path) -> None:
    """Replay must reproduce silver byte for byte, or bronze is not a replay foundation.

    This is what makes bronze worth keeping: given the same bronze, re-running the transform from
    scratch must yield the same silver. Anything non-deterministic in the transform — a timestamp, a
    random projection, dict iteration order — breaks the claim silently, because both runs "succeed"
    and only their contents differ.
    """
    import hashlib

    uri = _bronze(tmp_path, [1, 2, 3, 4])

    def replay_hash(target: str) -> str:
        rows = transform_batch(read_delta(uri, None))
        write_silver(target, rows, run_id="replay")
        table = lance.dataset(target).to_table().sort_by("id")
        # Hashed from Arrow IPC bytes, not via pandas: the runner is SEALED and ships no pandas, and
        # a hash that needs a dependency the runner does not carry cannot run where the runner runs.
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()

    first = replay_hash(str(tmp_path / "silver-a.lance"))
    second = replay_hash(str(tmp_path / "silver-b.lance"))

    assert first == second, "an E5 replay produced different silver from identical bronze"


def test_a19_a_delta_bounded_by_a_STALE_version_does_not_silently_skip(tmp_path: Path) -> None:
    """The boundary comes from the publication event, never from a fresh DescribeTable read.

    A mover that read the version itself could observe a version NEWER than the one it was woken for
    — another writer having committed in between — and would then skip every row in the gap. The rows
    are lost with no error anywhere: the hop reports success having processed nothing. Bounding by an
    older version must OVER-deliver (idempotency absorbs it), never under-deliver.
    """
    uri = _bronze(tmp_path, [1, 2, 3])
    v1 = lance.dataset(uri).version
    _append(uri, [4, 5])
    _append(uri, [6])

    # Woken for v1 but two commits have landed: every row after v1 must arrive, not just the last.
    assert sorted(read_delta(uri, v1).column("id").to_pylist()) == [4, 5, 6]
