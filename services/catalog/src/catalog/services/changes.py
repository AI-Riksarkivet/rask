"""The change-data-feed predicates — what a consumer needs to follow a table (§ J4).

Lance tracks per-row versions, and `lance_docs/file_format.md:4270-4300` gives the queries verbatim.
This module is the ONE place they are written, because the two windows are easy to get subtly wrong and
a wrong window does not fail — it answers rows, just the wrong ones, and the consumer cannot tell.

    inserted   _row_created_at_version > begin AND _row_created_at_version <= end
    updated    _row_created_at_version <= begin
               AND _row_last_updated_at_version > begin
               AND _row_last_updated_at_version <= end

THE `updated` FILTER'S FIRST CLAUSE IS THE SUBTLE ONE, and the guide states its purpose
(`file_format.md:4294`): "excludes newly inserted rows by requiring `_row_created_at_version <=
begin_version`". Drop it and every insert is reported twice — once as inserted, once as updated — so a
consumer applying both streams double-counts.

PRECONDITION, `file_format.md:4011-4015`: the version columns mean something only where stable row ids
are on, and otherwise the feed is SILENTLY EMPTY rather than an error. Measured on pylance 12.0.0: a
table created without `enable_stable_row_ids` still answers both columns, both read 1 for every row
after an append and an update, and the `inserted` window (1, 3] answers []. A consumer reads that as
"nothing changed", so `require_row_versions` refuses such a table before any predicate runs. The
catalog cannot rule the case out at creation alone: a table registered at an empty location, or one
whose bytes were rewritten outside the catalog, reaches this door without passing a create.

THE CONSUMER RULE, stated once for every published range (`table_published`'s and a stage trigger's
`{from_version, to_version}`, a work order's `version_floor`). A consumer resolves the window
`(from, to]` in one of two ways, and in no other:

    1. through `/changes` — `inserted`, `updated` and `deleted` for that window, applied together; or
    2. by merging on its key every row with `_row_last_updated_at_version > from` (and `<= to` when
       bounded), then retracting the rows `DatasetDelta.get_deleted_row_ids()` names for the window.

`_row_created_at_version` alone is the INSERTED predicate and nothing more: a consumer keyed on it never
sees an in-place correction or a deletion.

THE WINDOW IS ONE SNAPSHOT. Lance's feed is three queries against the table AT `end_version`
(`file_format.md:4270-4298`): a row updated inside the window and again after it carries the later
version, so a scan of the latest snapshot drops it from a closed window. The door opens the dataset at
`end_version` (at the head it read, for an open window) and answers all three kinds from that handle.

AND ONLY A WINDOW THE VERSION COLUMNS DESCRIBE. Three transactions change rows without moving those
columns the way the predicates assume, so the door reads the window's transactions and refuses a
window spanning one (`unfollowable`): a Restore and an Overwrite replace the table wholesale, and an
Update whose `fields_modified` is non-empty rewrote a column in place (`fragment.update_columns` plus
`LanceOperation.Update`, `lance_docs/guide.md:1715-1770`; measured on pylance 12.0.0 it leaves
`_row_last_updated_at_version` where it was, so the `updated` stream never shows the change).
`dataset.update` and `merge_insert` commit an Update with `fields_modified == []` and new fragments,
which the columns describe, and a compaction (a ReserveFragments BaseOperation then a Rewrite) moves
no row. A window from version 0 is the whole snapshot at `end_version`, which no transaction can make
wrong: `inserted` answers every row, `updated` none, and `deleted` none, since any row it could name
is one the consumer never held. So it is answered without the walk, and without `delta()`, which would
need version 1's manifest that maintenance cleanup removes. A later begin whose manifest is gone is
refused for every kind alike.

THE CASCADE ASKS THE SAME QUESTION WITH ONE COLUMN, and the difference is deliberate.
`scripts/ray_stage_job._delta_filter` filters on `_row_last_updated_at_version` alone, which selects
the inserted and the updated rows together — a never-updated row carries its creation version there
(measured 2026-09-11). It may collapse them because it merge-inserts whatever it selects; this module
may not, because a consumer applying both streams double-counts an insert reported as both.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

import lance
from lance import LanceOperation
from lance_namespace import InvalidInputError, InvalidTableStateError


#: The three questions a consumer can ask of a version window.
#:
#: `deleted` IS NOT LIKE THE OTHER TWO, and the asymmetry is the whole reason it is called out here.
#: The version columns describe rows the table STILL HAS, so `inserted` and `updated` are scan
#: predicates over them — while a deleted row is gone from the scan entirely and no predicate can name
#: it. Lance answers that question from the TRANSACTION range instead
#: (`DatasetDelta.get_deleted_row_ids()`, which streams a single `_rowid` column), so the door branches
#: on the kind rather than composing one filter for all three.
ChangeKind = Literal["inserted", "updated", "deleted"]

#: The two version columns Lance maintains per row. Named once: a typo in either is not an error, it is
#: an unresolved column at scan time, and the message names the column rather than the feature.
_CREATED = "_row_created_at_version"
_UPDATED = "_row_last_updated_at_version"

#: What the feed adds to every projection. `file_format.md:4283` states the documented result "includes
#: the version metadata columns" — Lance does NOT project pseudo-columns unless they are named, so the
#: door names them. `_rowid` rides along because it is what identifies the row an update applies TO: the
#: primary key is the transform's business, and the cascade's tiers do not all carry one.
FEED_COLUMNS: Final = (_CREATED, _UPDATED, "_rowid")


def require_row_versions(stable_row_ids: bool, *, table: str) -> None:
    """Refuse a change feed over a table whose row versions are not tracked.

    Raises:
        InvalidTableStateError: the table has no stable row ids (spec code 19, 409). The request is
            well formed and the table cannot answer it truthfully; stable row ids are create-time-only,
            so the remedy is a new table, not a retry.
    """
    if not stable_row_ids:
        raise InvalidTableStateError(
            f"table {table} was created without stable row ids, so Lance tracks no row versions and keeps no deleted-row record "
            "for it: a change feed would answer an empty window whatever changed. Recreate it with enable_stable_row_ids=True."
        )


def validate_window(*, begin_version: int, end_version: int | None) -> None:
    """Refuse a window no table can answer truthfully.

    Raises:
        InvalidInputError: for a negative begin, or a window that ends before it starts. Both would
            answer an EMPTY feed, which a consumer reads as "nothing changed" — the one answer a
            malformed request must never produce. The estate's 400: a bare `ValueError` here reached
            the live door's caller as `InternalError 18` (measured 2026-09-08), which says "the catalog
            is broken, retry" about a request that will fail identically forever.
    """
    if begin_version < 0:
        raise InvalidInputError(f"begin_version must be >= 0 (got {begin_version}) — version 0 is the empty dataset, so there is no wider window")
    if end_version is not None and end_version < begin_version:
        raise InvalidInputError(
            f"begin_version {begin_version} is after end_version {end_version} — an inverted window answers no rows, "
            "which is indistinguishable from 'nothing changed'"
        )


def unfollowable(transaction: lance.Transaction | None) -> str | None:
    """Why a window containing ``transaction`` cannot be answered from the version columns, or ``None``.

    See the module docstring's THE WINDOW rules. A version with no readable transaction is refused too:
    the door cannot vouch for a window whose history it cannot see.
    """
    if transaction is None:
        return "records no transaction"
    operation = transaction.operation
    if isinstance(operation, LanceOperation.Restore):
        return "is a Restore, which replaces the table with an earlier version"
    if isinstance(operation, LanceOperation.Overwrite):
        return "is an Overwrite, which replaces every row"
    if isinstance(operation, LanceOperation.Update) and operation.fields_modified:
        return f"rewrote field ids {list(operation.fields_modified)} in place, which moves no row's version"
    return None


def change_filter(*, begin_version: int, end_version: int | None, kind: ChangeKind) -> str:
    """The SQL predicate selecting rows that changed in ``(begin_version, end_version]``.

    ``end_version`` of ``None`` is the open window — "everything since" — which is the ordinary
    subscription shape. It must stay unbounded rather than defaulting to the begin version or to the
    dataset's current version read separately: a bound read at a different moment than the scan is a
    window that silently drops whatever landed in between.

    Raises:
        InvalidInputError: an invalid window (:func:`validate_window`), or ``kind="deleted"``.
    """
    validate_window(begin_version=begin_version, end_version=end_version)
    if kind == "deleted":
        # NOT A PREDICATE, AND SAYING SO IS THE POINT. The version columns describe rows the table still
        # has; a deleted row is absent from the scan, so any filter composed here would answer the wrong
        # rows with a 200 — the exact failure this module's header names ("it answers rows, just the
        # wrong ones, and the consumer cannot tell"). The door reads the transaction range instead.
        raise InvalidInputError(
            "a 'deleted' feed is not a scan predicate: the row is gone from the table, so no filter over "
            "the version columns can name it — the door serves it from the transaction range instead"
        )
    if kind == "inserted":
        clauses = [f"{_CREATED} > {begin_version}"]
        if end_version is not None:
            clauses.append(f"{_CREATED} <= {end_version}")
        return " AND ".join(clauses)
    clauses = [f"{_CREATED} <= {begin_version}", f"{_UPDATED} > {begin_version}"]
    if end_version is not None:
        clauses.append(f"{_UPDATED} <= {end_version}")
    return " AND ".join(clauses)


def feed_projection(columns: Sequence[str] | None, *, data_columns: Sequence[str]) -> list[str]:
    """The caller's projection plus the version columns it needs to CHECKPOINT.

    A change feed whose rows carry no version is a feed that can be read once: the consumer polls
    "what changed since N", receives rows, and has no N to send next — so it re-sends the old one and
    replays the same rows forever. Measured on the live door before this existed (2026-09-08,
    `bronze$pages`): three real changed rows, projected as `['id', 'payload', 'source_uri', 'stage']`,
    with nothing to advance on.

    `data_columns` is the dataset's own schema, passed in rather than read here, because this module
    owns the feed's vocabulary and deliberately not a dataset handle.

    A name the caller already asked for is not added twice — Lance refuses a duplicated projection, so
    a consumer that asks for `_rowid` correctly would otherwise have its request rejected for it.
    """
    chosen = list(columns) if columns is not None else list(data_columns)
    return chosen + [column for column in FEED_COLUMNS if column not in chosen]
