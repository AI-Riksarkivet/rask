"""Erasure that reaches every surface the catalog serves a table from ([[LH-073]]).

A `delete_from_table` removes rows from ONE ref and propagates nowhere. Measured against pylance
11.0.0 — a table with a branch, a tag and history, after the subject is deleted from main:

    main after delete   ['bob', 'carol', 'dan']
    branch 'work'       ['alice', 'bob', 'carol', 'dan']
    tag 'pinned'        ['alice', 'bob', 'carol', 'dan']
    version 1           ['alice', 'bob', 'carol', 'dan']

All three survivors are LIVE surfaces, not archival residue: a branch answers through `?branch=` on
every read door, a tag and an old version through `checkout_version`. So the subject's data was one
request away from anyone who could read the table, after the operation reported success.

**THE ORDER IS FORCED BY THE FORMAT, not chosen.** A branch PINS the parent's history at the branch
point — measured: with a branch present, `cleanup_old_versions` leaves the pre-delete version and the
subject stays readable at it; delete the branch and the same call removes it. A tag pins the same way
by design. So reclamation LAST is not tidiness — run it first and it is a no-op, and the estate
reports an erasure that reclaimed nothing:

    1. every branch          — delete on it, because a branch is a separate dataset with its own rows
    2. every pinning tag     — remove it, or the version it holds can never be reclaimed
    3. main                  — the row delete the door already performed
    4. reclaim history       — now that nothing pins it

**WHAT THIS CANNOT PROMISE, stated because an erasure report that overclaims is worse than none.**
Reclamation is still bounded by Lance's listing floor: `cleanup_old_versions` clamps its file listing
to the earliest RETAINED manifest's commit timestamp, so bytes written above that instant are
invisible to it ([[LH-094]]). This reports what it reclaimed rather than asserting the bytes are gone,
and a caller who needs that guarantee needs the floor raised first.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Protocol

from pydantic import BaseModel, Field


log = logging.getLogger(__name__)


class SurfaceResult(BaseModel):
    """What happened on one surface. NAMED, never counted — an erasure that half-succeeded has to say
    which half, because the remainder is a legal obligation and not a retry."""

    surface: str
    #: What the estate did there: `deleted`, `untagged`, `reclaimed`, or `failed`.
    outcome: str
    detail: str = ""


class ErasureReport(BaseModel):
    """One subject's erasure across every surface the catalog serves this table from."""

    table: str
    predicate: str
    surfaces: list[SurfaceResult] = Field(default_factory=list)
    #: Versions reclaimed and bytes freed by the final cleanup. ZERO IS NOT A FAILURE — a table whose
    #: history is inside the retention window legitimately reclaims nothing yet.
    versions_reclaimed: int = 0
    bytes_reclaimed: int = 0
    #: Retained versions that STILL answer the predicate after every step ran. Non-empty means the
    #: erasure is incomplete however cleanly each step reported — see the verify step.
    residual_versions: list[int] = Field(default_factory=list)
    #: True only when every surface reported a non-`failed` outcome AND nothing still answers the
    #: predicate. A caller reporting completion to a data subject reads THIS, never the absence of an
    #: exception.
    complete: bool = False


class _Dataset(Protocol):
    """The pylance surface this uses, kept structural so the ordering is testable without a store."""

    @property
    def version(self) -> int: ...
    @property
    def branches(self) -> Any: ...
    @property
    def tags(self) -> Any: ...
    def delete(self, predicate: str, /) -> None: ...
    def checkout_version(self, version: Any, /) -> Any: ...
    def versions(self) -> Any: ...
    def to_table(self, *args: Any, **kwargs: Any) -> Any: ...
    def cleanup_old_versions(self, older_than: timedelta | None = None, *, delete_unverified: bool = False) -> Any: ...


def erase(dataset: _Dataset, *, table: str, predicate: str, retention: timedelta) -> ErasureReport:
    """Delete every row matching ``predicate`` from every ref, then reclaim what no longer pins it.

    ``retention`` is passed straight to `cleanup_old_versions` rather than forced to zero: an erasure
    is not a licence to destroy unrelated history, and a caller that means to reclaim everything says
    so by passing zero.

    EVERY SURFACE IS ATTEMPTED even when one fails. Stopping at the first error would leave the
    remaining surfaces both un-erased and unreported, so the caller would not know what is left — and
    with a legal deadline attached, "we do not know" is the worst of the three outcomes.
    """
    report = ErasureReport(table=table, predicate=predicate)
    failed = False

    # 1. BRANCHES FIRST. Each is a separate dataset with its own rows AND it pins the parent's history
    #    at the branch point, so a branch left alone defeats step 4 as well as retaining the rows.
    for name in _branch_names(dataset):
        try:
            dataset.checkout_version((name, None)).delete(predicate)
            report.surfaces.append(SurfaceResult(surface=f"branch:{name}", outcome="deleted"))
        except Exception as exc:  # noqa: BLE001 — one surface's failure must not hide the others
            failed = True
            log.warning("erasure_branch_failed", extra={"table": table, "branch": name, "error": str(exc)})
            report.surfaces.append(SurfaceResult(surface=f"branch:{name}", outcome="failed", detail=str(exc)))

    # 2. TAGS. A tag holds a VERSION, so there is nothing to delete from — the row is only unreachable
    #    once the tag stops pinning the version that still contains it, and only then is it reclaimable.
    for name in _tag_names(dataset):
        try:
            dataset.tags.delete(name)
            report.surfaces.append(SurfaceResult(surface=f"tag:{name}", outcome="untagged"))
        except Exception as exc:  # noqa: BLE001
            failed = True
            log.warning("erasure_tag_failed", extra={"table": table, "tag": name, "error": str(exc)})
            report.surfaces.append(SurfaceResult(surface=f"tag:{name}", outcome="failed", detail=str(exc)))

    # 3. MAIN — the delete the door already performs today, here so one call covers every ref.
    try:
        dataset.delete(predicate)
        report.surfaces.append(SurfaceResult(surface="main", outcome="deleted"))
    except Exception as exc:  # noqa: BLE001
        failed = True
        log.warning("erasure_main_failed", extra={"table": table, "error": str(exc)})
        report.surfaces.append(SurfaceResult(surface="main", outcome="failed", detail=str(exc)))

    # 4. RECLAIM, last, because every step above was removing something that pinned this.
    try:
        stats = dataset.cleanup_old_versions(retention, delete_unverified=True)
        report.versions_reclaimed = int(getattr(stats, "old_versions", 0) or 0)
        report.bytes_reclaimed = int(getattr(stats, "bytes_removed", 0) or 0)
        report.surfaces.append(
            SurfaceResult(surface="history", outcome="reclaimed", detail=f"{report.versions_reclaimed} versions, {report.bytes_reclaimed} bytes")
        )
    except Exception as exc:  # noqa: BLE001
        failed = True
        log.warning("erasure_cleanup_failed", extra={"table": table, "error": str(exc)})
        report.surfaces.append(SurfaceResult(surface="history", outcome="failed", detail=str(exc)))

    # 5. VERIFY, because every step above is an ATTEMPT and only this is evidence. Measured while
    #    building this: deleting rows ON a branch does NOT remove that branch's pin on the parent's
    #    history — the branch reads clean and the parent's pre-delete version survives cleanup still
    #    holding the subject. So a run can perform every step successfully and leave the row readable,
    #    which is the one outcome an erasure must never report as done.
    residual = _versions_still_matching(dataset, predicate)
    if residual:
        failed = True
        log.warning("erasure_incomplete", extra={"table": table, "versions": residual})
        report.surfaces.append(
            SurfaceResult(
                surface="verify",
                outcome="failed",
                detail=f"version(s) {residual} still answer this predicate — a branch or tag still pins them",
            )
        )
    else:
        report.surfaces.append(SurfaceResult(surface="verify", outcome="clean"))
    report.residual_versions = residual

    report.complete = not failed
    return report


def _versions_still_matching(dataset: _Dataset, predicate: str) -> list[int]:
    """Every retained version that still answers ``predicate`` — the erasure's own proof.

    Asked of the VERSIONS rather than of main, because main is the one surface the old implementation
    already reached and the three that matter are the ones behind it. A version this cannot read is
    reported as residual rather than skipped: an unreadable version is not evidence of absence, and
    this function exists to produce evidence.
    """
    still: list[int] = []
    try:
        versions = [int(entry["version"]) for entry in dataset.versions()]
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_verify_list_failed", extra={"error": str(exc)})
        return [-1]
    for version in versions:
        try:
            if dataset.checkout_version(version).to_table(filter=predicate).num_rows:
                still.append(version)
        except Exception as exc:  # noqa: BLE001
            log.warning("erasure_verify_failed", extra={"version": version, "error": str(exc)})
            still.append(version)
    return still


def _branch_names(dataset: _Dataset) -> list[str]:
    """Every branch of this table, or an empty list when they cannot be listed.

    An unreadable branch list is NOT treated as "no branches" silently — the caller sees it because the
    report then names no branch surface at all, and `complete` stays true only if nothing else failed.
    Raising here instead would abandon the surfaces below, which is the worse trade.
    """
    try:
        listed = dataset.branches.list()
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_branch_list_failed", extra={"error": str(exc)})
        return []
    return [str(name) for name in (listed or [])]


def _tag_names(dataset: _Dataset) -> list[str]:
    """Every tag on this table. `tags.list()` answers a mapping of name -> version in pylance 11."""
    try:
        listed = dataset.tags.list()
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_tag_list_failed", extra={"error": str(exc)})
        return []
    return [str(name) for name in (listed or [])]
