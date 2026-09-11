"""`changes since version N` — the feed a BYO consumer needs to follow a table (§ J4).

A lakehouse buyer expects to subscribe to a table without re-reading it. Lance supports exactly that,
and `lance_docs/file_format.md:4270-4300` gives the predicates verbatim:

    inserted   _row_created_at_version > begin AND _row_created_at_version <= end
    updated    _row_created_at_version <= begin
               AND _row_last_updated_at_version > begin
               AND _row_last_updated_at_version <= end

THE ESTATE ALREADY ASKS THIS QUESTION AND KEPT IT PRIVATE. `ray_stage_job._delta_filter` drives the
cascade off `_row_last_updated_at_version`, so the mechanism is proven on this data — what it cannot
express is the two kinds SEPARATELY, which is what a consumer outside the cascade needs and what this
door serves.

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


# --------------------------------------------------------------------------- #
# LH-008: a DELETED row has to reach the consumer too
# --------------------------------------------------------------------------- #


def test_a_DELETED_row_is_not_expressible_as_a_SCAN_PREDICATE() -> None:
    """CONTRACT: `change_filter` refuses `deleted`, and says why rather than inventing a predicate.

    The two version columns describe rows the table STILL HAS. A deleted row is gone from the scan, so
    no predicate over `_row_created_at_version` / `_row_last_updated_at_version` can name it — Lance
    answers this question through `DatasetDelta.get_deleted_row_ids()` instead, which reads the
    transaction range rather than the rows.

    Refused here rather than silently returning an inserted/updated predicate: a feed that answers the
    WRONG rows with a 200 is the failure this module's own docstring exists to prevent — "it answers
    rows, just the wrong ones, and the consumer cannot tell".
    """
    with pytest.raises(InvalidInputError) as caught:
        changes.change_filter(begin_version=1, end_version=4, kind="deleted")
    message = str(caught.value).lower()
    assert "deleted" in message
    assert "predicate" in message or "scan" in message, caught.value


def test_the_DELETED_kind_is_part_of_the_feeds_vocabulary() -> None:
    """A consumer that cannot ask for deletions has to infer them, and inferring them is unbounded.

    Silver and gold served retracted rows indefinitely because the feed had no way to express the
    question: the publication delta is insert-only and the writer hard-deletes with
    `when_not_matched_by_source_delete`, so a row that left the tier left no trace a consumer could
    follow.

    SERVED ON DEMAND rather than stamped into the publish event (owner decision, 2026-09-11): a
    deleted-row set is unbounded, so stamping it makes a large delete a large message on the bus, and
    every change-data system of this shape — Debezium, Iceberg CDC, Delta CDF — publishes a version
    range and lets the consumer pull the rows it cares about.
    """
    from typing import get_args

    assert "deleted" in get_args(changes.ChangeKind), "the feed cannot express a deletion"


def test_a_deleted_row_COMES_BACK_from_the_transaction_range(tmp_path, monkeypatch) -> None:
    """The behaviour, not the plumbing: delete a row, and the feed names it.

    Driven against a real Lance dataset because the whole defect was that the question had no answer —
    asserting the door calls a function would restate the wiring rather than prove a retraction is
    followable. `_rowid` is all that comes back, and that is Lance's answer rather than a projection
    choice: the row is gone, so there is nothing else left to return.
    """
    import lance
    import pyarrow as pa

    uri = str(tmp_path / "t")
    lance.write_dataset(pa.table({"id": ["a", "b", "c"]}), uri, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
    ds = lance.dataset(uri)
    begin = ds.version
    rowid_of = dict(
        zip(
            ds.to_table(columns=["id"], with_row_id=True).column("id").to_pylist(),
            ds.to_table(columns=["id"], with_row_id=True).column("_rowid").to_pylist(),
            strict=True,
        )
    )
    lance.dataset(uri).delete("id = 'b'")

    # THROUGH THE DOOR, with the OPEN window a consumer actually sends. `delta()` refuses
    # `end_version=None` outright ("Must specify both with_begin_version and with_end_version"), so
    # closing it is the door's job and this is what proves the door does it.
    import pyarrow.ipc

    from catalog.services import dataplane

    monkeypatch.setattr(dataplane, "open_dataset", lambda *_a, **_kw: lance.dataset(uri))
    # CAST, not a suppression: `open_dataset` is patched above so the namespace is never touched, and
    # the cast says exactly that. A mypy-style suppression would be the wrong tool's syntax, and ty
    # parses one written even inside a comment — which is how this line first tripped the checker.
    from typing import cast

    from lance_namespace import LanceNamespace

    payload = dataplane.read_deleted_row_ids(cast("LanceNamespace", None), {}, ["t"], begin_version=begin, end_version=None)
    deleted = pyarrow.ipc.open_file(pa.py_buffer(payload)).read_all().column("_rowid").to_pylist()

    assert deleted == [rowid_of["b"]], f"the feed did not name the deleted row: {deleted} (b was {rowid_of['b']})"
    assert rowid_of["a"] not in deleted and rowid_of["c"] not in deleted, "a surviving row was reported as deleted"
