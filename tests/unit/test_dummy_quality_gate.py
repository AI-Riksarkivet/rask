"""The dummy lane's silver→gold quality gate — promote vs HOLD, decided before visibility.

Item (2) of the A11 lane. The gate machinery already exists (`quality_enabled`, `qualityKeyColumn`,
`quality_required_columns`, and `transform.py:336`'s `if assertions and not passed(assertions)`); what
was missing is a lane that exercises it with data anyone can produce. The dummy runner's silver is
exactly that — deterministic, GPU-free, and shaped like real silver.

These use `assert_quality_on_batch`, the PRE-COMMIT form, because D3 is the point: a held batch must
never become a version. The post-commit form leaves the bad rows in the tier and merely declines to
trigger the next stage, which is a much weaker claim than the gate appears to make.

The silver schema is restated here rather than imported: `runners/dummy` is SEALED (no workspace
glob), so tests/unit cannot import it. Restating a schema is the drift risk that buys — accepted here
because the alternative is un-sealing a runner, which is the precedent the estate deliberately
refuses. A shape mismatch surfaces immediately in the A11 lane.
"""

from __future__ import annotations

import pyarrow as pa
from medallion.services.quality import (
    COLUMN_DECLARED,
    NOT_NULL,
    ROW_COUNT_POSITIVE,
    assert_quality_on_batch,
    passed,
)


EMBED_DIM = 8

#: Mirrors dummy_runner.transform.SILVER_SCHEMA.
DUMMY_SILVER = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("source_rowid", pa.int64()),
        pa.field("checksum", pa.string()),
        pa.field("word_count", pa.int64()),
        pa.field("embedding", pa.list_(pa.float32(), EMBED_DIM)),
    ]
)

#: What a gold consumer DECLARES it reads. The gate refuses a promotion that dropped any of them —
#: turning a runtime failure in someone else's job into a refusal here.
GOLD_CONTRACT = ("id", "source_rowid", "checksum", "embedding")


def _silver(ids: list[int | None], *, drop: str | None = None) -> pa.Table:
    n = len(ids)
    columns = {
        "id": pa.array(ids, pa.int64()),
        "source_rowid": pa.array(list(range(n)), pa.int64()),
        "checksum": pa.array(["ab" * 32] * n, pa.string()),
        "word_count": pa.array([3] * n, pa.int64()),
        "embedding": pa.array([[0.1] * EMBED_DIM] * n, pa.list_(pa.float32(), EMBED_DIM)),
    }
    if drop:
        columns.pop(drop)
    fields = [f for f in DUMMY_SILVER if f.name in columns]
    return pa.table(columns, schema=pa.schema(fields))


def test_good_dummy_silver_promotes() -> None:
    """The happy path the A11 lane must take all the way to gold."""
    assertions = assert_quality_on_batch(_silver([1, 2, 3]), key_column="id", required_columns=GOLD_CONTRACT)
    assert passed(assertions)
    assert {a.assertion for a in assertions} == {ROW_COUNT_POSITIVE, NOT_NULL, COLUMN_DECLARED}


def test_an_empty_silver_is_HELD() -> None:
    """A transform that produced nothing must not promote an empty gold.

    An empty promotion is a silent failure: downstream sees a successful hop and zero rows, which
    reads as "there was nothing to do" rather than "the transform broke".
    """
    assert not passed(assert_quality_on_batch(_silver([]), key_column="id", required_columns=GOLD_CONTRACT))


def test_a_null_id_is_HELD() -> None:
    """A null identity means the join that produced this silver is broken.

    It would also break the merge_insert the next hop performs — merge on a null key silently
    mis-groups rows rather than failing.
    """
    failed = [a for a in assert_quality_on_batch(_silver([1, None, 3]), key_column="id", required_columns=GOLD_CONTRACT) if not a.success]
    assert [a.assertion for a in failed] == [NOT_NULL]


def test_dropping_a_column_gold_DECLARED_is_HELD() -> None:
    """The breaking-change detector on the lane's own contract.

    Dropping `source_rowid` is the interesting case: silver would still look fine on its own, and the
    failure would surface far downstream when a consumer tried to resolve bronze payloads through a
    reference that is no longer there (D2). Catching it at the gate turns a mystery into a refusal.
    """
    failed = [a for a in assert_quality_on_batch(_silver([1, 2], drop="source_rowid"), key_column="id", required_columns=GOLD_CONTRACT) if not a.success]
    assert [(a.assertion, a.column) for a in failed] == [(COLUMN_DECLARED, "source_rowid")]


def test_a_held_batch_reports_WHICH_assertion_failed() -> None:
    """The gate must say what was wrong, or an operator can only re-run and hope.

    A hold with no reason is the failure mode that gets a gate disabled rather than a batch fixed.
    """
    results = assert_quality_on_batch(_silver([]), key_column="id", required_columns=GOLD_CONTRACT)
    failures = [a for a in results if not a.success]

    assert failures, "a held batch must name at least one failed assertion"
    assert all(a.assertion for a in failures)


def test_the_restated_schema_matches_the_sealed_runners_own() -> None:
    """Guard the one risk this file's design accepts: a restated schema drifting from its source.

    `runners/dummy` is sealed out of the workspace, so this module cannot import its SILVER_SCHEMA and
    has to restate it. That is a real drift risk, taken deliberately — un-sealing a runner to avoid it
    would set the precedent the estate refuses (the next runner brings torch in with it).

    So the drift is detected instead of prevented: read the runner's source and compare field names.
    Text, not an import, because importing is precisely what sealing forbids.
    """
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "runners" / "dummy" / "src" / "dummy_runner" / "transform.py").read_text(encoding="utf-8")
    block = source.split("SILVER_SCHEMA = pa.schema(", 1)[1]
    declared = re.findall(r'pa\.field\("(\w+)"', block.split("]", 1)[0])

    assert declared == [f.name for f in DUMMY_SILVER], (
        f"the sealed runner's silver schema changed to {declared} — this file's copy is stale, and the "
        f"A11 gate is now asserting against a shape the lane no longer produces"
    )
