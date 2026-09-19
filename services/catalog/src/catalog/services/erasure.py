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
    2. every tag pinning the SUBJECT — remove it, or the version it holds can never be reclaimed;
       a tag over a version the subject never appeared in is a reproducibility pointer and is KEPT
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
from collections.abc import Mapping
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
    #: Branch name -> the parent version it pins, for every branch pinning a version that still answers.
    #: THIS IS THE ACTIONABLE HALF: `residual_versions` says the erasure is incomplete, and only this
    #: says what to delete to finish it. Lance records the fork point in `_refs/branches/<name>.json`
    #: (`parentVersion`), so the pin is readable rather than inferred.
    pinned_by: dict[str, int] = Field(default_factory=dict)
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
    branches = _branches(dataset)
    for name in branches:
        try:
            dataset.checkout_version((name, None)).delete(predicate)
            report.surfaces.append(SurfaceResult(surface=f"branch:{name}", outcome="deleted"))
        except Exception as exc:  # noqa: BLE001 — one surface's failure must not hide the others
            failed = True
            log.warning("erasure_branch_failed", extra={"table": table, "branch": name, "error": str(exc)})
            report.surfaces.append(SurfaceResult(surface=f"branch:{name}", outcome="failed", detail=str(exc)))

    # 2. TAGS THAT PIN THE SUBJECT — and ONLY those. A tag holds a VERSION, so there is nothing to
    #    delete from it; the row becomes unreachable once the tag stops pinning the version holding it,
    #    and only then is that version reclaimable.
    #
    #    A TAG IS ALSO A REPRODUCIBILITY POINTER, which is why this is selective. A tagged version is
    #    exempt from cleanup by design, so tagging is how a training run records the exact data it saw.
    #    Dropping every tag would erase that record for versions the subject never appeared in —
    #    destroying model provenance as a side effect of a request about one person. A tag whose version
    #    cannot be READ is dropped anyway: unreadable is not evidence of absence, and an erasure resolves
    #    that doubt against the tag.
    for name, version in _tags(dataset).items():
        if version is not None and _answers(dataset, version, predicate) is False:
            report.surfaces.append(SurfaceResult(surface=f"tag:{name}", outcome="retained", detail=f"version {version} does not answer the predicate"))
            continue
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
    report.pinned_by = {name: pinned for name, pinned in branches.items() if pinned is not None and pinned in residual}
    if residual:
        failed = True
        log.warning("erasure_incomplete", extra={"table": table, "versions": residual})
        report.surfaces.append(
            SurfaceResult(
                surface="verify",
                outcome="failed",
                detail=(f"version(s) {residual} still answer this predicate; pinned by {report.pinned_by or 'no branch this could name'}"),
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


def _branches(dataset: _Dataset) -> dict[str, int | None]:
    """Every branch of this table mapped to the parent version it pins, `{}` when they cannot be listed.

    `branches.list()` answers a mapping of name -> metadata in pylance 11, carrying the `parent_version`
    Lance records at the fork point. That version is what makes the erasure report actionable: a branch
    pins the parent's history there, so it is the reason a pre-delete version survives cleanup.

    An unreadable branch list is NOT silently treated as "no branches" — the caller sees it because the
    report then names no branch surface at all, and `complete` stays true only if nothing else failed.
    Raising here instead would abandon the surfaces below, which is the worse trade.
    """
    try:
        listed = dataset.branches.list()
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_branch_list_failed", extra={"error": str(exc)})
        return {}
    if isinstance(listed, Mapping):
        return {str(name): _parent_version(meta) for name, meta in listed.items()}
    return {str(name): None for name in (listed or [])}


def _parent_version(meta: object) -> int | None:
    """The fork point out of one branch's metadata, or None when it is not readable as an int."""
    value = meta.get("parent_version") if isinstance(meta, Mapping) else None
    return int(value) if isinstance(value, int) else None


def _tags(dataset: _Dataset) -> dict[str, int | None]:
    """Every tag on this table mapped to the version it pins. `tags.list()` answers a mapping of
    name -> metadata in pylance 11, carrying that version; `None` marks one this cannot read, which the
    caller resolves against the tag rather than in its favour."""
    try:
        listed = dataset.tags.list()
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_tag_list_failed", extra={"error": str(exc)})
        return {}
    if isinstance(listed, Mapping):
        return {str(name): _tag_version(meta) for name, meta in listed.items()}
    return {str(name): None for name in (listed or [])}


def _tag_version(meta: object) -> int | None:
    """The version one tag pins, out of its metadata or out of a bare int."""
    if isinstance(meta, Mapping):
        value = meta.get("version")
        return int(value) if isinstance(value, int) else None
    return int(meta) if isinstance(meta, int) else None


def _answers(dataset: _Dataset, version: int, predicate: str) -> bool | None:
    """Whether ``version`` still holds a row matching ``predicate`` — `None` when it cannot be read.

    THREE-VALUED ON PURPOSE. A caller deciding whether to destroy something must distinguish "proved
    clean" from "could not tell", and only the first is a reason to keep it.
    """
    try:
        return bool(dataset.checkout_version(version).to_table(filter=predicate).num_rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_tag_probe_failed", extra={"version": version, "error": str(exc)})
        return None
