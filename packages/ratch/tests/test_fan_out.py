"""Fan-out: one input row becomes MANY output rows, in a different table.

This is the shape a chunked medium needs — a video into frames, an audio file into segments, a
document into passages. `StageShape.APPEND_ROWS` declares it and `run_append_rows_stage` implements
it, and until now nothing exercised it: `packages/ratch` shipped no tests at all, so the one driver
whose entire purpose is changing cardinality had never been observed changing it.

The assertions are about CARDINALITY and IDENTITY, because those are what fan-out can get wrong in
ways that still look successful:

* the output must be LARGER than the input — a row-preserving stage would pass every schema check
  and every lineage assertion while quietly not fanning out at all;
* it lands in a DIFFERENT table, so the source tier keeps its own grain;
* a re-run must not double the rows. Resume here is a key diff, not a merge, so a source row already
  represented in the output is dropped before the actor sees it — and an append-only writer that got
  that wrong would silently duplicate every chunk on every redelivery.
"""

from __future__ import annotations

from collections.abc import Callable

import pyarrow as pa
import pytest
from ratch.core.driver import run_append_rows_stage
from ratch.core.registry import Stage, StageShape


lance = pytest.importorskip("lance")
pytest.importorskip("lance_ray")

CHUNKS_PER_DOC = 4
OUTPUT_SCHEMA = pa.schema([pa.field("doc_id", pa.int64()), pa.field("chunk_no", pa.int64()), pa.field("text", pa.string())])


def _chunker() -> Callable[[pa.Table], pa.Table]:
    """One document row -> CHUNKS_PER_DOC rows. The stand-in for frames or segments."""

    def fan(batch: pa.Table) -> pa.Table:
        doc_ids, chunk_nos, texts = [], [], []
        for doc_id, body in zip(batch["doc_id"].to_pylist(), batch["body"].to_pylist(), strict=True):
            for n in range(CHUNKS_PER_DOC):
                doc_ids.append(doc_id)
                chunk_nos.append(n)
                texts.append(f"{body}#{n}")
        return pa.table({"doc_id": doc_ids, "chunk_no": chunk_nos, "text": texts}, schema=OUTPUT_SCHEMA)

    return fan


@pytest.fixture
def db(tmp_path):
    """A `docs` table of 3 rows — the input grain."""
    (tmp_path / "docs.lance").parent.mkdir(parents=True, exist_ok=True)
    lance.write_dataset(
        pa.table({"doc_id": [1, 2, 3], "body": ["alpha", "beta", "gamma"]}),
        str(tmp_path / "docs.lance"),
    )
    return tmp_path


def _stage() -> Stage:
    return Stage(
        name="chunk_docs",
        shape=StageShape.APPEND_ROWS,
        table="docs",
        read_columns=("body",),
        key_columns=("doc_id",),
        output_table="chunks",
    )


def _run(db) -> int:
    return run_append_rows_stage(
        db,
        _stage(),
        factory=_chunker,
        output_schema=OUTPUT_SCHEMA,
        create_output=lambda: lance.write_dataset(OUTPUT_SCHEMA.empty_table(), str(db / "chunks.lance")),
    )


def test_one_row_in_becomes_many_rows_out(db) -> None:
    """The whole point. A row-preserving stage passes every other check in this file."""
    appended = _run(db)
    assert appended == 3 * CHUNKS_PER_DOC, appended

    out = lance.dataset(str(db / "chunks.lance"))
    assert out.count_rows() == 12
    src = lance.dataset(str(db / "docs.lance"))
    assert out.count_rows() > src.count_rows(), "the output did not fan out"


def test_the_fan_out_lands_in_a_DIFFERENT_table(db) -> None:
    """The source tier keeps its own grain — 3 documents stay 3 documents."""
    _run(db)
    assert lance.dataset(str(db / "docs.lance")).count_rows() == 3


def test_every_input_row_is_represented(db) -> None:
    _run(db)
    out = lance.dataset(str(db / "chunks.lance")).to_table()
    assert sorted(set(out["doc_id"].to_pylist())) == [1, 2, 3]
    assert sorted(out.to_pydict()["chunk_no"][:4]) == list(range(CHUNKS_PER_DOC))


def test_a_RERUN_does_not_duplicate(db) -> None:
    """Resume is a key diff: a doc already chunked is dropped before the actor runs.

    An append-only writer that skipped this would double every chunk on each redelivery, and the row
    count is the only place that shows.
    """
    first = _run(db)
    second = _run(db)
    assert first == 12
    assert second == 0, f"a re-run appended {second} rows — the key diff did not hold"
    assert lance.dataset(str(db / "chunks.lance")).count_rows() == 12
