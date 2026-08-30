"""An unreadable predecessor and an absent one both ask — so the LOG has to tell them apart.

Both helpers return `None` on failure, and `None` means FIRST_PROMOTION, which means ASK. That default
is right: a dataset whose history cannot be read should get a person's attention rather than a silent
promote. But it collapses two very different situations into one outcome — "there is no predecessor"
and "I could not read the predecessor" — and an operator reading `first_promotion` in a review has no
way to know which happened.

`writing-python` → `anti-patterns.md` § Bare exception handling names exactly this: the fix for a
broad catch is "catch specific exceptions; LOG or re-raise". The broad catch stays (any unreadable
destination genuinely is "no comparable predecessor"), but it must not be silent.
"""

from __future__ import annotations

import logging

import pytest

from medallion.services.compute import existing_row_count
from medallion.services.promotion_band import previous_row_count


def test_an_unreadable_destination_returns_none_and_says_why(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert existing_row_count("s3://nonexistent-bucket-xyz/nothing.lance", {}) is None
    assert caplog.records, "the read failed and nothing said so — 'ask' is indistinguishable from 'blind'"


def test_the_predecessor_read_also_says_why(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert previous_row_count("s3://nonexistent-bucket-xyz/nothing.lance", {}, version=9) is None
    assert caplog.records, "same collapse, in the sibling that feeds the band directly"


def test_a_genuinely_absent_predecessor_is_not_an_error(caplog: pytest.LogCaptureFixture) -> None:
    """Version 1 has no predecessor by definition — that is normal, and must not look like a fault."""
    with caplog.at_level(logging.WARNING):
        assert previous_row_count("s3://anywhere/x.lance", {}, version=1) is None
    assert not caplog.records, "a first version is not a failure; warning here would cry wolf on every new dataset"
