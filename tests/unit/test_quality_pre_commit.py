"""D3 — the quality gate runs BEFORE visibility, and the window it closes.

Today's gate validates a dataset that already exists (`transform.py:324-337, :462-468`), so a failed
assertion leaves the commit in place and merely skips the next-stage trigger. The bad batch IS in the
tier; it just does not propagate. Anyone reading that table directly, or resolving `latest` instead of
the published tag, sees data the gate rejected.

These tests pin the pre-commit form: the same assertions, the same names, the same `passed()` — run
on the batch, so a rejected batch never becomes a version at all.
"""

from __future__ import annotations

import pyarrow as pa

from service_kit.lakehouse.quality import (
    COLUMN_DECLARED,
    NOT_NULL,
    ROW_COUNT_POSITIVE,
    assert_quality_on_batch,
    passed,
)


SCHEMA = pa.schema([pa.field("id", pa.int64()), pa.field("text", pa.string())])


def _batch(ids: list[int | None], texts: list[str | None] | None = None) -> pa.Table:
    return pa.table(
        {"id": pa.array(ids, pa.int64()), "text": pa.array(texts or ["t"] * len(ids), pa.string())},
        schema=SCHEMA,
    )


def test_a_good_batch_passes() -> None:
    assert passed(assert_quality_on_batch(_batch([1, 2, 3]), key_column="id"))


def test_an_empty_batch_is_refused_before_it_becomes_a_version() -> None:
    """An empty promotion is a silent failure — and pre-commit it never becomes a version at all."""
    results = assert_quality_on_batch(_batch([]), key_column="id")

    assert not passed(results)
    assert [a.assertion for a in results if not a.success] == [ROW_COUNT_POSITIVE]


def test_a_null_key_is_refused() -> None:
    """A null in the identity column means a broken join or transform upstream."""
    results = assert_quality_on_batch(_batch([1, None, 3]), key_column="id")

    failed = [a for a in results if not a.success]
    assert [a.assertion for a in failed] == [NOT_NULL]
    assert failed[0].column == "id"


def test_a_dropped_declared_column_is_refused() -> None:
    """The breaking-change detector, moved pre-commit.

    Schema-on-write stays free — additive evolution is never blocked. What is stopped is COMMITTING a
    version that dropped a column a downstream consumer declared it reads, which turns a runtime
    failure in someone else's job into a refusal here.
    """
    results = assert_quality_on_batch(_batch([1, 2]), key_column="id", required_columns=("id", "confidence"))

    failed = [a for a in results if not a.success]
    assert [a.assertion for a in failed] == [COLUMN_DECLARED]
    assert failed[0].column == "confidence"


def test_a_missing_key_column_is_skipped_not_failed() -> None:
    """Different stages key differently — absence is not a violation.

    Failing here would make the gate refuse every stage whose data legitimately has no such column,
    which is how a gate gets disabled wholesale rather than fixed.
    """
    results = assert_quality_on_batch(_batch([1, 2]), key_column="page_id")

    assert passed(results)
    assert NOT_NULL not in [a.assertion for a in results]


def test_the_pre_commit_and_post_commit_gates_agree_on_the_same_batch(tmp_path: object) -> None:
    """The two forms must not drift on what 'good' means.

    The estate already shares `blob_column_resolves` between the gate and the reconcile sweep for
    exactly this reason — one probe, two enforcement points. The same has to hold for the structural
    assertions, or a batch could pass pre-commit and fail post-publish on identical data.
    """
    import lance

    from service_kit.lakehouse.quality import assert_quality

    batch = _batch([1, 2, 3])
    uri = str(tmp_path) + "/q.lance"  # type: ignore[operator]
    lance.write_dataset(batch, uri, mode="create")

    pre = assert_quality_on_batch(batch, key_column="id", required_columns=("id",))
    post = assert_quality(uri, {}, key_column="id", required_columns=("id",))

    structural = {(a.assertion, a.column, a.success) for a in pre}
    post_structural = {(a.assertion, a.column, a.success) for a in post if a.assertion != "blob_resolves"}
    assert structural == post_structural, "the two gates disagree on identical data"
