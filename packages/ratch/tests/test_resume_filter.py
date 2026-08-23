"""B4's second half — the resume predicate that reads `transform_version`.

`driver.py`'s own header states the principle: "resume is a property of the read, not bookkeeping".
The predicate was `<output> IS NULL`, which asks only "was this row ever computed". With a transform
identity it can ask the question B4 is actually for: "was this row computed by THIS transform".

The degradation matters as much as the feature. A dataset written before the column existed has no
`transform_version` to compare, and a predicate naming a missing column is a scan error, not a
resume — so the widened form appears only when the column is actually present. That keeps the change
backward-compatible with every dataset already on disk, which is the constraint the plan flagged:
the column cannot be retrofitted onto history.
"""

from __future__ import annotations

from ratch.core.driver import TRANSFORM_VERSION_COLUMN, resume_filter


def test_without_a_version_column_it_is_the_original_predicate() -> None:
    """Every dataset written before B4 — the predicate must not name a column that is not there."""
    assert resume_filter("vector", identity="abc123", has_version_column=False) == "vector IS NULL"


def test_without_an_identity_it_is_the_original_predicate() -> None:
    """A caller that cannot compute an identity gets the old behaviour rather than a wrong one."""
    assert resume_filter("vector", identity="", has_version_column=True) == "vector IS NULL"


def test_with_both_it_also_claims_rows_from_a_stale_transform() -> None:
    predicate = resume_filter("vector", identity="abc123", has_version_column=True)
    assert "vector IS NULL" in predicate
    assert f"{TRANSFORM_VERSION_COLUMN} != 'abc123'" in predicate
    assert predicate.startswith("(") and predicate.endswith(")"), "must be parenthesised — it is ANDed with the media gate"


def test_a_null_version_is_named_explicitly() -> None:
    """A NULL transform_version (a row written before the column) must COUNT as stale.

    `!= 'x'` is NULL for a NULL left side and a NULL predicate excludes the row, so the NULL case needs
    its own disjunct. SQL's one-word form is `IS DISTINCT FROM` — which LANCE REJECTS ("not supported
    SQL in lance"), caught by executing the predicate in test_resume_filter_against_lance.py. Hence the
    long form, which is not a style choice and must not be "simplified" back.
    """
    predicate = resume_filter("vector", identity="abc123", has_version_column=True)
    assert f"{TRANSFORM_VERSION_COLUMN} IS NULL" in predicate
    assert "IS DISTINCT FROM" not in predicate


def test_the_identity_is_quoted_into_the_predicate() -> None:
    """Identities are hex from sha256, so quoting is sufficient — but it must actually be quoted."""
    assert "'abc123'" in resume_filter("vector", identity="abc123", has_version_column=True)
