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

import pytest
from lance_namespace import InvalidInputError

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


def test_a_BEGIN_AFTER_END_is_refused_AS_A_CLIENT_ERROR() -> None:
    """An inverted window answers empty, which reads as "nothing changed" — the one answer a consumer
    must never receive when its request was malformed.

    `InvalidInputError`, not `ValueError`, and the difference is what the caller sees. Driven against
    the deployed door 2026-09-08, a bare `ValueError` reached the client as `InternalError 18` — a 500,
    which tells a consumer the CATALOG is broken and to retry, when the truth is that its own request
    was malformed and retrying it will fail identically forever.
    """
    with pytest.raises(InvalidInputError, match="begin_version"):
        changes.change_filter(begin_version=9, end_version=7, kind="inserted")


def test_a_NEGATIVE_begin_is_refused_AS_A_CLIENT_ERROR() -> None:
    """Version 0 is the empty dataset, so "since -1" is not a wider window, it is a malformed one."""
    with pytest.raises(InvalidInputError):
        changes.change_filter(begin_version=-1, end_version=None, kind="inserted")


def test_the_feed_PROJECTS_the_version_columns_so_a_consumer_can_CHECKPOINT() -> None:
    """`file_format.md:4283`: the query returns the changed rows "including the version metadata columns
    `_row_created_at_version`, `_row_last_updated_at_version`, and `_rowid`".

    THIS IS THE WHOLE FEED, not a nicety. A consumer polls "what changed since N" and must learn the N
    to send next; without a version on the rows it has no way to advance, and re-sending its old N
    replays the same rows forever. Measured on the live door 2026-09-08 before this existed: a feed over
    `bronze$pages` answered `['id', 'payload', 'source_uri', 'stage']` — three real rows and nothing to
    checkpoint from. Lance does not project the pseudo-columns unless they are asked for, so the door
    asks.
    """
    got = changes.feed_projection(None, data_columns=["id", "payload"])
    assert got[:2] == ["id", "payload"]
    for column in ("_row_created_at_version", "_row_last_updated_at_version", "_rowid"):
        assert column in got, f"a consumer cannot checkpoint without {column}"


def test_a_CALLERS_column_list_still_gets_the_version_columns() -> None:
    """Narrowing the payload is the ordinary case for a wide table, and it must not cost the ability to
    advance — the version columns ride along with whatever was asked for."""
    got = changes.feed_projection(["id"], data_columns=["id", "payload"])
    assert got[0] == "id"
    assert "payload" not in got, "a caller's projection was widened"
    assert "_row_created_at_version" in got


def test_a_CALLER_who_names_a_version_column_gets_it_ONCE() -> None:
    """Lance rejects a duplicated name in a projection, so a consumer that asks for the column the door
    adds anyway would have its request refused for asking correctly."""
    got = changes.feed_projection(["_rowid", "id"], data_columns=["id"])
    assert got.count("_rowid") == 1, f"duplicated projection: {got}"
