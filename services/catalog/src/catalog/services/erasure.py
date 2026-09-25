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

**EVERY REF HAS ITS OWN HISTORY.** A branch keeps its own `_versions/` and `data/` under
`tree/<name>/` (`lance_docs/file_format.md` "Branch Dataset Layout"), numbered in its own line, so a
subject written ON a branch lives in versions main's cleanup never lists. Rewrite, reclaim and verify
therefore run once per ref, through that ref's own handle.

**THE ORDER IS FORCED BY THE FORMAT, not chosen.** A branch PINS the version it was cut from, and a tag
pins the version it names. Measured on pylance 12.0.0, the fork pin holds exactly as long as a retained
version of the branch references that version's files: cleanup on the parent keeps it while one does
(its manifest, and only the files still referenced) and takes it once none does. So reclamation LAST is
not tidiness — run it first and it is a no-op, and the estate reports an erasure that reclaimed nothing —
and the refs are rewritten and reclaimed DEEPEST FIRST, main last, so each parent is reclaimed after the
branches standing on it have let go:

    1. every branch          — delete on its head, because a branch is a separate dataset with its own rows
    2. every tag pinning the SUBJECT — remove it, or the version it holds can never be reclaimed;
       a tag over a version the subject never appeared in is a reproducibility pointer and is KEPT
    3. main                  — the row delete the door already performed
    4. rewrite every ref     — compact, so the subject's bytes leave the live data files
    5. reclaim every ref     — now that nothing the erasure controls pins them
    6. verify every ref      — the evidence `complete` is computed from

A pin that survives — a retained branch version still on the subject's files — is REPORTED, never
deleted: deleting the branch destroys someone's working ref ([[LH-178]]). `pinned_by` names what to
delete, in an order Lance accepts.

**WHAT THIS CANNOT PROMISE, stated because an erasure report that overclaims is worse than none.**
Reclamation is still bounded by Lance's listing floor: `cleanup_old_versions` clamps its file listing
to the earliest RETAINED manifest's commit timestamp, so bytes written above that instant are
invisible to it ([[LH-094]]). This reports what it reclaimed rather than asserting the bytes are gone,
and a caller who needs that guarantee needs the floor raised first.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, Protocol

from pydantic import BaseModel, Field

from catalog.services.dataplane import MAIN_BRANCH, recorded_branch
from catalog.services.maintenance import COMPACTION_BOUND


log = logging.getLogger(__name__)


class SurfaceResult(BaseModel):
    """What happened on one surface. NAMED, never counted — an erasure that half-succeeded has to say
    which half, because the remainder is a legal obligation and not a retry."""

    surface: str
    #: What the estate did there: `deleted`, `untagged`, `retained`, `rewritten`, `reclaimed`, `clean`,
    #: or `failed`.
    outcome: str
    detail: str = ""


class Pin(BaseModel):
    """One ref to delete before the erasure can finish, in the order ``ErasureReport.pinned_by`` gives."""

    #: `branch:<name>` or `tag:<name>`.
    ref: str
    #: The version it stands on — a branch's fork point, a tag's version — as `<ref>@<version>`. One not
    #: in `residual_versions` is named because Lance refuses to delete a later entry while it exists.
    holds: str


class ErasureReport(BaseModel):
    """One subject's erasure across every surface the catalog serves this table from."""

    table: str
    predicate: str
    surfaces: list[SurfaceResult] = Field(default_factory=list)
    #: Versions reclaimed and bytes freed across every ref's cleanup. ZERO IS NOT A FAILURE — a table
    #: whose history is inside the retention window legitimately reclaims nothing yet.
    versions_reclaimed: int = 0
    bytes_reclaimed: int = 0
    #: Retained versions of ANY ref that still answer the predicate, as `<ref>@<version>` with `main` for
    #: main. Unambiguous: Lance reserves `main` and admits no `@` in a branch name (`lance_docs/file_format.md`
    #: "Branch Name"). Non-empty means the erasure is incomplete however cleanly each step reported.
    residual_versions: list[str] = Field(default_factory=list)
    #: THE ACTIONABLE HALF: what to delete to finish, in an order Lance accepts — tags, then branches
    #: deepest first. Lance refuses to delete a branch another branch is cut from or a tag names (measured
    #: on pylance 12.0.0), so a branch pinning a residual brings its descendants and their tags. Delete
    #: them in order, then erase again. Fork points are read from `_refs/branches/<name>.json`, not inferred.
    pinned_by: list[Pin] = Field(default_factory=list)
    #: True only when no surface reported `failed` — `verify` among them, which fails whenever a residual
    #: exists or a ref could not be listed. A caller reporting completion to a data subject reads THIS,
    #: never the absence of an exception.
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
    def count_rows(self, *args: Any, **kwargs: Any) -> Any: ...
    @property
    def optimize(self) -> Any: ...
    def cleanup_old_versions(
        self, older_than: timedelta | None = None, *, delete_unverified: bool = False, error_if_tagged_old_versions: bool = True
    ) -> Any: ...


def erase(dataset: _Dataset, *, table: str, predicate: str, retention: timedelta) -> ErasureReport:
    """Delete every row matching ``predicate`` from every ref, then reclaim what no longer pins it.

    ``dataset`` is a handle on main; every branch is reached through it. ``retention`` is passed straight
    to each ref's `cleanup_old_versions` rather than forced to zero: an erasure is not a licence to destroy
    unrelated history, and a caller that means to reclaim everything says so by passing zero.

    EVERY SURFACE IS ATTEMPTED even when one fails. Stopping at the first error would leave the
    remaining surfaces both un-erased and unreported, so the caller would not know what is left — and
    with a legal deadline attached, "we do not know" is the worst of the three outcomes.
    """
    report = ErasureReport(table=table, predicate=predicate)

    # 1. BRANCHES FIRST. Each is a separate dataset with its own rows AND it pins its parent's history
    #    at the fork point, so a branch left alone defeats step 5 as well as retaining the rows.
    listed = _branches(dataset)
    if listed is None:
        report.surfaces.append(
            SurfaceResult(surface="branches", outcome="failed", detail="the branch list could not be read, so no branch was erased, reclaimed or verified")
        )
    branches = listed or {}
    for name in branches:
        try:
            dataset.checkout_version((name, None)).delete(predicate)
            report.surfaces.append(SurfaceResult(surface=f"branch:{name}", outcome="deleted"))
        except Exception as exc:  # noqa: BLE001 — one surface's failure must not hide the others
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
    #
    #    The probe reads the tag's OWN ref. A tag names a version of the branch it records, and branch
    #    histories are numbered independently, so judging a `work` tag by main's same-numbered version
    #    would keep a tag pinning the subject and drop a clean one.
    for name, reference in _tags(dataset).items():
        if reference is not None and _answers(dataset, reference, predicate) is False:
            report.surfaces.append(SurfaceResult(surface=f"tag:{name}", outcome="retained", detail=f"{_name(reference)} does not answer the predicate"))
            continue
        try:
            dataset.tags.delete(name)
            report.surfaces.append(SurfaceResult(surface=f"tag:{name}", outcome="untagged"))
        except Exception as exc:  # noqa: BLE001
            log.warning("erasure_tag_failed", extra={"table": table, "tag": name, "error": str(exc)})
            report.surfaces.append(SurfaceResult(surface=f"tag:{name}", outcome="failed", detail=str(exc)))

    # 3. MAIN — the delete the door already performs today, here so one call covers every ref.
    try:
        dataset.delete(predicate)
        report.surfaces.append(SurfaceResult(surface="main", outcome="deleted"))
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_main_failed", extra={"table": table, "error": str(exc)})
        report.surfaces.append(SurfaceResult(surface="main", outcome="failed", detail=str(exc)))

    forks = {name: fork for name, fork in branches.items() if fork is not None}
    deepest_first: list[str | None] = [*sorted(branches, key=lambda name: (-_depth(name, forks), name)), None]

    # 4. COMPACT EVERY REF, because a predicate delete writes a DELETION FILE and leaves the data file
    #    live for the rows that survive — so the erased subject's bytes stay on storage, and a blob
    #    column's payload sidecar with them. Measured on pylance 11.0.0 with two 5 MiB blob rows: after
    #    the delete the directory is still 10.5 MB and `cleanup_old_versions` frees 1,188 bytes;
    #    compacting first rewrites the fragment without the row, and the same cleanup then frees 10.5 MB.
    #    A branch-owned file is rewritten only through the branch's handle: measured on pylance 12.0.0,
    #    it still held the subject after the branch's cleanup until the branch was compacted first. That
    #    rewrite also copies the inherited fragments it selects under `tree/<name>/` — the parent's files
    #    are untouched — which is what lets step 5 release the fork pin.
    #
    #    Best-effort like every other surface: a ref that cannot be compacted still gets its rows deleted
    #    and its history reclaimed, and the report says the bytes may remain rather than implying they
    #    are gone.
    for ref in deepest_first:
        surface = f"compact:{_label(ref)}"
        try:
            # THE SAME BOUND THE COMPACT DOOR CARRIES, imported rather than restated: this pod has more
            # than one button that reaches `compact_files`, and an erasure runs over exactly the tables
            # most likely to hold a blob column — so the unbounded default is not the cheaper path here.
            _head(dataset, ref).optimize.compact_files(**COMPACTION_BOUND)
            report.surfaces.append(SurfaceResult(surface=surface, outcome="rewritten"))
        except Exception as exc:  # noqa: BLE001
            log.warning("erasure_compact_failed", extra={"table": table, "ref": _label(ref), "error": str(exc)})
            report.surfaces.append(SurfaceResult(surface=surface, outcome="failed", detail=f"{exc} — the subject's bytes may remain in a live data file"))

    # 5. RECLAIM EVERY REF, last, because every step above was removing something that pinned this, and
    #    DEEPEST FIRST: cleanup through a branch handle removes only that branch's own versions and files
    #    (`test_the_gc_run_reclaims_the_ref_the_request_names`), and the parent's cleanup then sees what
    #    the branch still references. Measured on pylance 12.0.0 on a chain main v2 <- work <- deeper, all
    #    rewritten: parent first keeps main v2 and work v3 holding the subject; deepest first reclaims both.
    for ref in deepest_first:
        surface = f"history:{_label(ref)}"
        try:
            # `error_if_tagged_old_versions=False` SKIPS a tagged version instead of failing the whole call.
            # Found by driving the deployed door: step 2 deliberately keeps a tag over a version the subject
            # never appeared in, and pylance's default then refuses the entire cleanup over that one tag —
            # so retaining a reproducibility pointer would cost the estate every byte of reclamation, and
            # the erasure would report `history: failed` for having done the right thing one step earlier.
            stats = _head(dataset, ref).cleanup_old_versions(retention, delete_unverified=True, error_if_tagged_old_versions=False)
            versions, freed = int(getattr(stats, "old_versions", 0) or 0), int(getattr(stats, "bytes_removed", 0) or 0)
            report.versions_reclaimed += versions
            report.bytes_reclaimed += freed
            report.surfaces.append(SurfaceResult(surface=surface, outcome="reclaimed", detail=f"{versions} versions, {freed} bytes"))
        except Exception as exc:  # noqa: BLE001
            log.warning("erasure_cleanup_failed", extra={"table": table, "ref": _label(ref), "error": str(exc)})
            report.surfaces.append(SurfaceResult(surface=surface, outcome="failed", detail=str(exc)))

    # 6. VERIFY EVERY REF, because every step above is an ATTEMPT and only this is evidence. A run can
    #    perform every step successfully and leave the row readable at a version a branch or a tag stands
    #    on, which is the one outcome an erasure must never report as done.
    found = _versions_still_matching(dataset, [None, *sorted(branches)], predicate)
    report.residual_versions = found.residual
    # Re-listed AFTER step 2, so these are the survivors: a tag whose delete failed pins its version
    # exactly as hard as a branch does.
    report.pinned_by = _pins(forks, _tags(dataset), found.residual)
    if found.residual or found.unlisted:
        log.warning("erasure_incomplete", extra={"table": table, "versions": found.residual, "unlisted": found.unlisted})
        report.surfaces.append(SurfaceResult(surface="verify", outcome="failed", detail=_verify_detail(found, report.pinned_by)))
    else:
        report.surfaces.append(SurfaceResult(surface="verify", outcome="clean"))

    report.complete = all(surface.outcome != "failed" for surface in report.surfaces)
    return report


class _Verification(BaseModel):
    """What step 6 found, per ref."""

    #: Every retained version that answers the predicate OR could not be read, as `<ref>@<version>`.
    residual: list[str] = Field(default_factory=list)
    #: The part of `residual` that could not be read at all.
    unreadable: list[str] = Field(default_factory=list)
    #: Refs whose versions could not be listed, so none of them was probed.
    unlisted: list[str] = Field(default_factory=list)


def _versions_still_matching(dataset: _Dataset, refs: Sequence[str | None], predicate: str) -> _Verification:
    """Every retained version of every ref that still answers ``predicate`` — the erasure's own proof.

    Asked of each ref's VERSIONS rather than of its head, because a head is the one surface the delete
    already reached. A version this cannot read is residual rather than skipped: unreadable is not
    evidence of absence, and this function exists to produce evidence. Measured on pylance 12.0.0, one
    arises from Lance itself: when a branch still references only some of its fork version's files, the
    parent's cleanup keeps that version's manifest and deletes the rest, and reading it fails.
    """
    found = _Verification()
    for ref in refs:
        try:
            versions = [int(entry["version"]) for entry in _head(dataset, ref).versions()]
        except Exception as exc:  # noqa: BLE001
            log.warning("erasure_verify_list_failed", extra={"ref": _label(ref), "error": str(exc)})
            found.unlisted.append(_label(ref))
            continue
        for version in versions:
            try:
                # COUNTED, not materialised. The question is "does this version still hold the subject",
                # and building the matching rows to read `.num_rows` sizes the PROOF by the erasure —
                # paid once per retained version, and worst exactly when the subject has the most rows.
                if dataset.checkout_version((ref, version)).count_rows(filter=predicate):
                    found.residual.append(_name((ref, version)))
            except Exception as exc:  # noqa: BLE001
                log.warning("erasure_verify_failed", extra={"ref": _label(ref), "version": version, "error": str(exc)})
                found.residual.append(_name((ref, version)))
                found.unreadable.append(_name((ref, version)))
    return found


def _verify_detail(found: _Verification, pins: Sequence[Pin]) -> str:
    """The failed verify surface's detail: what is left, and what to delete to finish."""
    answering = [version for version in found.residual if version not in found.unreadable]
    parts = [f"{answering} still answer this predicate"] if answering else []
    if found.unreadable:
        parts.append(f"{found.unreadable} could not be read")
    if found.unlisted:
        parts.append(f"the versions of {found.unlisted} could not be listed")
    parts.append(f"delete {[pin.ref for pin in pins]} in that order, then erase again" if pins else "no ref this could name pins them")
    return "; ".join(parts)


def _pins(forks: Mapping[str, _Reference], tags: Mapping[str, _Reference | None], residual: Sequence[str]) -> list[Pin]:
    """What to delete for ``residual`` to become reclaimable, in an order Lance accepts.

    ``forks`` maps each branch to the version it was cut from. A branch cut from, or a tag naming, a
    residual version pins it. Measured on pylance 12.0.0, `branches.delete` refuses a branch another
    branch is cut from ("Branch work is referenced by [("deeper", 3)] versions") or a tag names
    ("referenced by tags [("t", 2)]") — even after the erasure has rewritten and reclaimed them — so a
    pinning branch brings every descendant and every tag on them: tags first, then branches deepest first.
    """
    held = set(residual)
    doomed = {name for name, fork in forks.items() if _name(fork) in held}
    frontier = list(doomed)
    while frontier:
        parent = frontier.pop()
        children = {name for name, fork in forks.items() if fork[0] == parent} - doomed
        doomed |= children
        frontier.extend(children)
    tag_pins = [Pin(ref=f"tag:{name}", holds=_name(ref)) for name, ref in sorted(tags.items()) if ref is not None and (_name(ref) in held or ref[0] in doomed)]
    deepest_first = sorted(doomed, key=lambda name: (-_depth(name, forks), name))
    return [*tag_pins, *(Pin(ref=f"branch:{name}", holds=_name(forks[name])) for name in deepest_first)]


def _depth(name: str, forks: Mapping[str, _Reference]) -> int:
    """How many branches ``name`` descends from, so a child always sorts before its parent.

    A branch whose fork point could not be read counts as cut from main. Bounded by the branch count:
    Lance's fork points form a tree, and a malformed ``_refs`` must not hang the erasure door.
    """
    depth, parent = 0, forks[name][0] if name in forks else None
    while parent is not None and parent in forks and depth < len(forks):
        depth, parent = depth + 1, forks[parent][0]
    return depth


#: Lance's global version identifier, ``(branch, version)`` with ``None`` naming main as
#: :func:`recorded_branch` normalises it — the form ``checkout_version`` takes ("Use
#: `(branch_name, version_number)` tuples as global identifiers", ``lance_docs/guide.md`` Branches),
#: because a bare version number means "on the handle's branch".
type _Reference = tuple[str | None, int]


def _label(ref: str | None) -> str:
    return MAIN_BRANCH if ref is None else ref


def _name(reference: _Reference) -> str:
    """The report's spelling of ``reference``: ``<ref>@<version>``."""
    branch, version = reference
    return f"{_label(branch)}@{version}"


def _head(dataset: _Dataset, ref: str | None) -> _Dataset:
    """A handle on ``ref``'s latest version — ``dataset`` itself for main, which the erasure is opened on."""
    return dataset if ref is None else dataset.checkout_version((ref, None))


def _branches(dataset: _Dataset) -> dict[str, _Reference | None] | None:
    """Every branch of this table mapped to the ``(parent branch, version)`` it was cut from, None when unlisted.

    `branches.list()` answers a mapping of name -> metadata carrying the `parent_branch` and
    `parent_version` Lance records at the fork point (measured on pylance 12.0.0; ``parentBranch`` /
    ``parentVersion`` in ``lance_docs/file_format.md`` "Branch Metadata File Format"). That reference is
    what makes the erasure report actionable: a branch pins its parent's history there, so it is the
    reason a pre-delete version survives cleanup.

    An unreadable branch list is None rather than `{}`: the caller cannot erase, reclaim or verify a ref
    it cannot name, so it records a failed surface. Raising here instead would abandon the surfaces
    below, which is the worse trade.
    """
    try:
        listed = dataset.branches.list()
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_branch_list_failed", extra={"error": str(exc)})
        return None
    if isinstance(listed, Mapping):
        return {str(name): _reference(meta, branch_key="parent_branch", version_key="parent_version") for name, meta in listed.items()}
    return {str(name): None for name in (listed or [])}


def _tags(dataset: _Dataset) -> dict[str, _Reference | None]:
    """Every tag on this table mapped to the version it pins, ON THE BRANCH IT NAMES.

    `tags.list()` is root-scoped: from any handle it answers every branch's tags, each carrying the
    `branch` it names (`null` for main — ``lance_docs/file_format.md`` "Tag File Format"). `None` marks
    one this cannot read, which the caller resolves against the tag rather than in its favour.
    """
    try:
        listed = dataset.tags.list()
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_tag_list_failed", extra={"error": str(exc)})
        return {}
    if isinstance(listed, Mapping):
        return {str(name): _reference(meta, branch_key="branch", version_key="version") for name, meta in listed.items()}
    return {str(name): None for name in (listed or [])}


def _reference(meta: object, *, branch_key: str, version_key: str) -> _Reference | None:
    """The ``(branch, version)`` one ref's metadata names, or None when it does not say."""
    if not isinstance(meta, Mapping):
        return None
    branch, version = meta.get(branch_key), meta.get(version_key)
    if (branch is not None and not isinstance(branch, str)) or not isinstance(version, int):
        return None
    return (recorded_branch(branch), version)


def _answers(dataset: _Dataset, reference: _Reference, predicate: str) -> bool | None:
    """Whether ``reference`` still holds a row matching ``predicate`` — `None` when it cannot be read.

    THREE-VALUED ON PURPOSE. A caller deciding whether to destroy something must distinguish "proved
    clean" from "could not tell", and only the first is a reason to keep it.
    """
    try:
        return bool(dataset.checkout_version(reference).count_rows(filter=predicate))
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_tag_probe_failed", extra={"reference": _name(reference), "error": str(exc)})
        return None
