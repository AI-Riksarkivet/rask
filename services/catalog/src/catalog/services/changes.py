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

PRECONDITION, `file_format.md:4015`: these columns exist only where row-level version tracking is on.
This estate requires it everywhere (`enable_stable_row_ids`; the catalog refuses a governed dataset
created without it), so the feed is available wherever the catalog governs — but a dataset registered
from outside that rule would answer an unresolved-column error rather than an empty feed, which is the
honest failure and is left to surface.

THE CASCADE ASKS THE SAME QUESTION WITH ONE COLUMN, and the difference is deliberate.
`scripts/ray_stage_job._delta_filter` filters on `_row_last_updated_at_version` alone, which selects
the inserted and the updated rows together — a never-updated row carries its creation version there
(measured 2026-09-11). It may collapse them because it merge-inserts whatever it selects; this module
may not, because a consumer applying both streams double-counts an insert reported as both.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from lance_namespace import InvalidInputError


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


def change_filter(*, begin_version: int, end_version: int | None, kind: ChangeKind) -> str:
    """The SQL predicate selecting rows that changed in ``(begin_version, end_version]``.

    ``end_version`` of ``None`` is the open window — "everything since" — which is the ordinary
    subscription shape. It must stay unbounded rather than defaulting to the begin version or to the
    dataset's current version read separately: a bound read at a different moment than the scan is a
    window that silently drops whatever landed in between.

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
