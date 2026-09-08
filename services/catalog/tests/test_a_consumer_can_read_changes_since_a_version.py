"""`changes since version N` — the feed a BYO consumer needs to follow a table (§ J4).

A lakehouse buyer expects to subscribe to a table without re-reading it. Lance supports exactly that,
and `lance_docs/file_format.md:4270-4300` gives the predicates verbatim:

    inserted   _row_created_at_version > begin AND _row_created_at_version <= end
    updated    _row_created_at_version <= begin
               AND _row_last_updated_at_version > begin
               AND _row_last_updated_at_version <= end

THE ESTATE ALREADY USES HALF OF IT AND KEEPS IT PRIVATE. `ray_stage_job._delta_filter` builds the
INSERTED predicate to drive the cascade, so the mechanism is proven on this data — what is missing is a
door, and the UPDATED half, which no consumer can express without it.

`file_format.md:4015` is the precondition: the version columns exist only when row-level version
tracking is on. This estate requires it already (`enable_stable_row_ids`, gate A14, which the catalog
refuses a governed dataset without), so the feed is available wherever the catalog governs.

A CHANGE FEED IS A READ, which decides both of its policy questions: it is gated like one and it is
audited like one (§ J1). A consumer that can follow every row a table ever received is doing the most
disclosing read available, not a metadata lookup.
"""

from __future__ import annotations

from catalog.services import changes


def test_the_INSERTED_predicate_is_the_documented_one() -> None:
    """Verbatim from `file_format.md:4281`. A bound that is off by one either replays a version or skips
    one, and a consumer cannot tell which from the rows it receives."""
    assert changes.change_filter(begin_version=7, end_version=9, kind="inserted") == ("_row_created_at_version > 7 AND _row_created_at_version <= 9")


def test_the_UPDATED_predicate_excludes_rows_that_were_INSERTED_in_the_window() -> None:
    """`file_format.md:4294`, and its reason: "This query excludes newly inserted rows by requiring
    `_row_created_at_version <= begin_version`". Without that clause an insert is reported twice — once
    as inserted and once as updated — and a consumer applying both double-counts it."""
    got = changes.change_filter(begin_version=7, end_version=9, kind="updated")
    assert "_row_created_at_version <= 7" in got, "an inserted row would also be reported as updated"
    assert "_row_last_updated_at_version > 7" in got
    assert "_row_last_updated_at_version <= 9" in got


def test_an_OPEN_end_means_everything_since() -> None:
    """The common subscription: "what changed since I last read". An absent end must not become a bound
    at 0 or at the begin version, either of which answers an empty feed forever."""
    got = changes.change_filter(begin_version=7, end_version=None, kind="inserted")
    assert got == "_row_created_at_version > 7", f"an open-ended feed was bounded: {got!r}"


def test_a_BEGIN_AFTER_END_is_refused() -> None:
    """An inverted window answers empty, which reads as "nothing changed" — the one answer a consumer
    must never receive when its request was malformed."""
    import pytest

    with pytest.raises(ValueError, match="begin_version"):
        changes.change_filter(begin_version=9, end_version=7, kind="inserted")


def test_a_NEGATIVE_begin_is_refused() -> None:
    """Version 0 is the empty dataset, so "since -1" is not a wider window, it is a malformed one."""
    import pytest

    with pytest.raises(ValueError):
        changes.change_filter(begin_version=-1, end_version=None, kind="inserted")
