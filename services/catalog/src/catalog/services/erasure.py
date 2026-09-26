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

**EVERY REF HAS ITS OWN HISTORY.** A branch keeps its own `_versions/` under `tree/<name>/`
(`lance_docs/file_format.md` "Branch Dataset Layout"), numbered in its own line, and writes the data it
adds under `tree/<name>/data/` (measured on pylance 12.0.0: `tracked_files()` places a branch's own
data files there, and `test_the_subjects_bytes_leave_the_branchs_own_data_files` finds the subject's
bytes in one). So a subject written ON a branch lives in versions main's cleanup never lists, and
rewrite, reclaim and verify run once per ref, through that ref's own handle.

**THE RECLAIM ORDER IS FORCED BY THE FORMAT, not chosen.** A branch PINS the version it was cut from, and
a tag pins the version it names. Measured on pylance 12.0.0, the fork pin holds exactly as long as a
retained version of the branch references that version's files: cleanup on the parent keeps it while
one does (its manifest, and only the files still referenced) and takes it once none does. So
reclamation LAST is not tidiness — run it first and it is a no-op, and the estate reports an erasure
that reclaimed nothing — and the refs are reclaimed DEEPEST FIRST, main last, so each parent is
reclaimed after the branches standing on it have let go. (Compaction runs in the same order; its own
order changes nothing measured.)

**A BRANCH'S REWRITE COPIES WHAT IT INHERITS, deliberately.** Compacting through a branch handle writes
the inherited fragments it selects into `tree/<name>/data/` — up to `COMPACTION_BOUND`'s 64 MiB per ref —
not only the fragments that held the subject, and each `compact:<ref>` surface reports the bytes. The
compact door refuses that materialisation as a COST (`maintenance.require_compactable`); here it is what
releases the fork pin, because the parent's cleanup takes the subject's version only once no branch
version stands on its files.

**A TABLE ANOTHER DATASET RESOLVES ITS FILES THROUGH IS NEITHER REWRITTEN NOR RECLAIMED** (#114, the
compact and GC doors' own gate): its reclaim would delete the only copy a shallow clone reads, and the
clone's own copy of the subject is its own surface ([[LH-263]]). Every ref's `compact:` and `history:`
surface then fails, and the residuals are reported as not reclaimed rather than attributed to a ref
whose deletion could not finish the erasure.

**THE DOORS' MANIFEST-FLAG GATES RUN PER REF**, before its rewrite and before its reclaim, and a refusal
fails that ref's surface the same way: a flag no rewrite here has been checked against (data overlays,
mixed file versions, anything unknown) is not compacted or reclaimed, and a shallow clone is reclaimed
but not rewritten into its own root. The one allowance is a branch's bases inside this table, which its
rewrite copies from on purpose (above; :func:`_compaction_refusal`).

    1. every branch          — delete on its head, because a branch is a separate dataset with its own rows
    2. every tag pinning the SUBJECT — remove it, or the version it holds can never be reclaimed;
       a tag over a version the subject never appeared in is a reproducibility pointer and is KEPT
    3. main                  — the row delete the door already performed
    4. rewrite every ref     — compact, so the subject's bytes leave the live data files
    5. reclaim every ref     — now that nothing the erasure controls pins them
    6. verify every ref      — the evidence `complete` is computed from

A version that survives is REPORTED with what keeps it, never deleted. `pinned_by` names the tags,
and a branch only when its own head still stands on the version's files, since deleting a branch
destroys someone's working ref ([[LH-178]]); `held_by_retention` names what the retention window keeps.

**WHAT THIS CANNOT PROMISE, stated because an erasure report that overclaims is worse than none.**
Reclamation is still bounded by Lance's listing floor: `cleanup_old_versions` clamps its file listing
to the earliest RETAINED manifest's commit timestamp, so bytes written above that instant are
invisible to it ([[LH-094]]). This reports what it reclaimed rather than asserting the bytes are gone,
and a caller who needs that guarantee needs the floor raised first. Nor does it reclaim a file no
version references that is younger than 7 days — an aborted write's residue — because Lance cannot
tell it from an in-flight write's (`lance_docs/lance_sdk.md` cleanup_old_versions `delete_unverified`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from lance_namespace import UnsupportedOperationError
from pydantic import BaseModel, Field

from catalog.services.dataplane import MAIN_BRANCH, recorded_branch
from catalog.services.maintenance import COMPACTION_BOUND, refuse_a_referring_datasets_source
from service_kit.lakehouse.base_refs import BaseRefs, normalise
from service_kit.lakehouse.features import (
    FLAG_BASE_PATHS,
    CompactionBases,
    data_file_base_paths,
    describe_compaction_unsupported_flags,
    describe_gc_unsupported_flags,
    gather_compaction_bases,
    manifest_feature_flags,
)
from service_kit.lakehouse.objectfs import StorageOptions, dataset_root_probe


log = logging.getLogger(__name__)


class SurfaceResult(BaseModel):
    """What happened on one surface. NAMED, never counted — an erasure that half-succeeded has to say
    which half, because the remainder is a legal obligation and not a retry."""

    surface: str = Field(
        description="`branch:<name>`, `tag:<name>`, `main`, `compact:<ref>`, `history:<ref>`, `dangling:<ref>@<version>`, `branches` or `verify`."
    )
    outcome: str = Field(
        description=(
            "What the estate did there: `deleted`, `untagged`, `retained`, `rewritten`, `reclaimed`, `clean`, `failed`, or `dangling` — a "
            "listed version that fails to read and is proved not to hold the subject; it stays listed until what its detail names lets go."
        )
    )
    detail: str = Field(default="", description="Why, in words: the error, what a rewrite cost, what a residual needs.")


class Pin(BaseModel):
    """One ref to delete before the erasure can finish, in the order ``ErasureReport.pinned_by`` gives."""

    ref: str = Field(description="`branch:<name>` or `tag:<name>`.")
    #: Rationale for the second case: a branch another branch is cut from, or a tag names, is refused
    #: deletion by Lance while that entry exists (measured on pylance 12.0.0).
    holds: str = Field(
        description=(
            "The version it stands on — a branch's fork point, a tag's version — as `<ref>@<version>`. One not in `residual_versions` "
            "keeps one that is through a branch version still on its files, or must go first because Lance refuses to delete a later entry."
        )
    )


class ErasureReport(BaseModel):
    """One subject's erasure across every surface the catalog serves this table from."""

    table: str = Field(description="The table id the erasure was asked of.")
    predicate: str = Field(description="The filter every matching row was deleted by.")
    surfaces: list[SurfaceResult] = Field(default_factory=list, description="Every surface the erasure attempted, in the order it attempted them.")
    versions_reclaimed: int = Field(
        default=0, description="Versions reclaimed across every ref's cleanup. Zero is not a failure: history inside the retention window stays."
    )
    bytes_reclaimed: int = Field(default=0, description="Bytes freed across every ref's cleanup.")
    #: Unambiguous: Lance reserves `main` and admits no `@` in a branch name (`lance_docs/file_format.md`
    #: "Branch Name").
    residual_versions: list[str] = Field(
        default_factory=list,
        description=(
            "Retained versions of any ref that still answer the predicate or could not be read, as `<ref>@<version>` with `main` for main. "
            "Non-empty means the erasure is incomplete however cleanly each step reported."
        ),
    )
    #: Fork points are read from `_refs/branches/<name>.json`; a branch is named only when its head stands
    #: on a residual's files, since deleting one destroys someone's working ref ([[LH-178]]).
    pinned_by: list[Pin] = Field(
        default_factory=list,
        description=(
            "The refs whose deletion lets the next erasure reclaim a residual, in an order Lance accepts: tags, then branches deepest first. "
            "A tag is named when it holds a residual, directly or through a branch version on its files; a branch only when its head does, "
            "with its descendants and their tags. A residual whose ref was not reclaimed names nothing here: its `history:` surface says why."
        ),
    )
    held_by_retention: list[str] = Field(
        default_factory=list,
        description=(
            "Residuals the retention window keeps, itself or through a newer branch version on their files: an erasure with "
            "`retain_days=0`, or one after the window has passed, reclaims them."
        ),
    )
    complete: bool = Field(
        default=False,
        description=(
            "True only when no surface failed, `verify` among them. It can be True beside `dangling:` surfaces, which hold no subject "
            "data but stay listed and fail to read. A caller reporting completion to a data subject reads this, never the status code."
        ),
    )


class _Dataset(Protocol):
    """The pylance surface this uses, kept structural so the ordering is testable without a store."""

    @property
    def uri(self) -> str: ...
    @property
    def _ds(self) -> Any: ...
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
    def cleanup_old_versions(self, older_than: timedelta | None = None, *, error_if_tagged_old_versions: bool = True) -> Any: ...
    def get_fragments(self) -> Any: ...
    def tracked_files(self, *, min_version: int | None = None) -> Any: ...
    def all_files(self) -> Any: ...


def erase(
    dataset: _Dataset,
    *,
    reopen: Callable[[], _Dataset],
    storage_options: StorageOptions,
    protected: BaseRefs | None,
    table: str,
    predicate: str,
    retention: timedelta,
) -> ErasureReport:
    """Delete every row matching ``predicate`` from every ref, then reclaim what no longer pins it.

    ``dataset`` is a handle on main; every branch is reached through it. ``storage_options`` binds the
    compact gate's base probe to the store the table lives in, which is why it has no default (see
    ``maintenance.require_compactable``). ``protected`` is the #114
    pre-pass's map of roots other datasets resolve their files through (``base_refs.sibling_base_refs``),
    or None when the caller collected none; it has no default so every caller says which. ``reopen``
    opens main again on a Lance session nothing has read through, for the verification: a session that
    has read a version keeps answering it after cleanup deletes its files (measured on pylance 12.0.0: a
    count through the door's shared session returned the subject from a version whose only copy was
    gone, and a new session raised "Not found"). An open that fails is a failed ``verify`` surface, not
    an exception. ``retention`` is passed straight to each ref's
    `cleanup_old_versions` rather than forced to zero: an erasure is not a licence to destroy unrelated
    history, and a caller that means to reclaim everything says so by passing zero.

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

    deepest_first: list[str | None] = [*sorted(branches, key=lambda name: (-_depth(name, branches), name)), None]

    # 4. COMPACT EVERY REF, because a predicate delete writes a DELETION FILE and leaves the data file
    #    live for the rows that survive — so the erased subject's bytes stay on storage, and a blob
    #    column's payload sidecar with them. Measured on pylance 11.0.0 with two 5 MiB blob rows: after
    #    the delete the directory is still 10.5 MB and `cleanup_old_versions` frees 1,188 bytes;
    #    compacting first rewrites the fragment without the row, and the same cleanup then frees 10.5 MB.
    #    A branch-owned file is rewritten only through the branch's handle: measured on pylance 12.0.0,
    #    it still held the subject after the branch's cleanup until the branch was compacted first. That
    #    rewrite also copies the inherited fragments it selects under `tree/<name>/` — the parent's files
    #    are untouched — which is what lets step 5 release the fork pin, and what the detail prices.
    #
    #    Best-effort like every other surface: a ref that cannot be compacted still gets its rows deleted
    #    and its history reclaimed, and the report says the bytes may remain rather than implying they
    #    are gone. The #114 answer is read once: a branch handle reports the dataset root as its `uri`
    #    (measured on pylance 12.0.0: `main.uri == branch.uri`), so it is the same answer for every ref.
    referred = _referred(dataset, protected)
    if referred is not None:
        log.warning("erasure_refused_referred_source", extra={"table": table, "reason": referred})
    for ref in deepest_first:
        surface = f"compact:{_label(ref)}"
        if referred is not None:
            report.surfaces.append(SurfaceResult(surface=surface, outcome="failed", detail=f"{referred} Not rewritten, so the subject's bytes remain."))
            continue
        try:
            handle = _head(dataset, ref)
            if (refused := _compaction_refusal(handle, storage_options)) is not None:
                log.warning("erasure_compact_refused", extra={"table": table, "ref": _label(ref), "reason": refused})
                report.surfaces.append(
                    SurfaceResult(surface=surface, outcome="failed", detail=f"maintenance refused: {refused}. Not rewritten, so the subject's bytes remain.")
                )
                continue
            report.surfaces.append(SurfaceResult(surface=surface, outcome="rewritten", detail=_compact(handle)))
        except Exception as exc:  # noqa: BLE001
            log.warning("erasure_compact_failed", extra={"table": table, "ref": _label(ref), "error": str(exc)})
            report.surfaces.append(SurfaceResult(surface=surface, outcome="failed", detail=f"{exc} — the subject's bytes may remain in a live data file"))

    # 5. RECLAIM EVERY REF, last, because every step above was removing something that pinned this, and
    #    DEEPEST FIRST: cleanup through a branch handle removes only that branch's own versions and files
    #    (`test_the_gc_run_reclaims_the_ref_the_request_names`), and the parent's cleanup then sees what
    #    the branch still references. Measured on pylance 12.0.0 on a chain main v2 <- work <- deeper, all
    #    rewritten: parent first keeps main v2 and work v3 holding the subject; deepest first reclaims both.
    #    Each ref's cutoff is read AFTER its cleanup returns: Lance measures its own during the call
    #    (measured on pylance 12.0.0), so ours is never the earlier one, and a version newer than it was
    #    inside that cleanup's window. A ref absent from `cutoffs` was not reclaimed, so nothing but that
    #    keeps its versions.
    cutoffs: dict[str | None, datetime] = {}
    for ref in deepest_first:
        surface = f"history:{_label(ref)}"
        if referred is not None:
            report.surfaces.append(SurfaceResult(surface=surface, outcome="failed", detail=f"{referred} Not reclaimed."))
            continue
        try:
            # `error_if_tagged_old_versions=False` SKIPS a tagged version instead of failing the whole call.
            # Found by driving the deployed door: step 2 deliberately keeps a tag over a version the subject
            # never appeared in, and pylance's default then refuses the entire cleanup over that one tag —
            # so retaining a reproducibility pointer would cost the estate every byte of reclamation, and
            # the erasure would report `history:<ref>` failed for having done the right thing one step earlier.
            #
            # NO `delete_unverified`: at retention 0 it deletes another writer's staged, uncommitted files
            # on this ref, and that writer's commit then lands a head that cannot be read (measured on
            # pylance 12.0.0; `lance_docs/guide.md` Cleanup calls the pair "extremely dangerous").
            handle = _head(dataset, ref)
            if (refused := describe_gc_unsupported_flags(*manifest_feature_flags(handle))) is not None:
                log.warning("erasure_cleanup_refused", extra={"table": table, "ref": _label(ref), "reason": refused})
                report.surfaces.append(SurfaceResult(surface=surface, outcome="failed", detail=f"maintenance refused: {refused}. Not reclaimed."))
                continue
            stats = handle.cleanup_old_versions(retention, error_if_tagged_old_versions=False)
            cutoffs[ref] = datetime.now(UTC) - retention
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
    #    Branches are listed AGAIN: one cut while the erasure ran — from a version the deletes had not yet
    #    reached — holds the subject at its head, and a list read in step 1 would never probe it.
    #    The open is guarded like every surface: steps 1-5 have already destroyed history, so an open
    #    that raised would lose their report and answer as if nothing had happened.
    try:
        cold = reopen()
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_verify_unopened", extra={"table": table, "error": str(exc)})
        detail = f"the verification could not open the table on a fresh session: {exc}; nothing was verified"
        report.surfaces.append(SurfaceResult(surface="verify", outcome="failed", detail=detail))
    else:
        _verify(report, _History(cold, cutoffs), first_listing=listed, predicate=predicate)

    report.complete = all(surface.outcome != "failed" for surface in report.surfaces)
    return report


def _verify(report: ErasureReport, history: _History, *, first_listing: Mapping[str, _Reference | None] | None, predicate: str) -> None:
    """Step 6: probe every retained version of every ref on ``history``'s cold handle, and record on
    ``report`` what survives, what keeps it, and the ``verify`` surface."""
    branches = first_listing or {}
    relisted = _branches(history.cold)
    current = branches if relisted is None else relisted
    found = _versions_still_matching(history, [None, *sorted({*branches, *current})], predicate)
    found.relist_failed = relisted is None
    found.late = sorted(set(current) - set(branches)) if first_listing is not None else []
    report.residual_versions = found.residual
    # Re-listed AFTER step 2, so these are the survivors: a tag whose delete failed holds its version
    # exactly as hard as a branch does.
    forks = {name: fork for name, fork in current.items() if fork is not None}
    tags = _tags(history.cold)
    report.surfaces.extend(
        SurfaceResult(surface=f"dangling:{name}", outcome="dangling", detail=_dangling_detail(_holders(_parse(name), history, forks, tags)))
        for name in found.dangling
    )
    account = _account(found.residual, history, forks, tags)
    account.unforked = sorted(name for name, fork in current.items() if fork is None) if found.residual else []
    report.pinned_by, report.held_by_retention = account.pins, account.held_by_retention
    if found.residual or found.unlisted or found.relist_failed:
        log.warning("erasure_incomplete", extra={"table": report.table, "versions": found.residual, "unlisted": found.unlisted})
        report.surfaces.append(SurfaceResult(surface="verify", outcome="failed", detail=_verify_detail(found, account)))
    else:
        report.surfaces.append(SurfaceResult(surface="verify", outcome="clean"))


class _Verification(BaseModel):
    """What step 6 found, per ref."""

    #: Every retained version that answers the predicate OR could not be read, as `<ref>@<version>`.
    residual: list[str] = Field(default_factory=list)
    #: The part of `residual` that could not be read at all.
    unreadable: list[str] = Field(default_factory=list)
    #: Listed versions that cannot be read whole, proved not to hold the subject fragment by fragment.
    dangling: list[str] = Field(default_factory=list)
    #: Refs whose versions could not be listed, so none of them was probed.
    unlisted: list[str] = Field(default_factory=list)
    #: The branch list could not be read again for the verification, so a branch cut meanwhile was not.
    relist_failed: bool = False
    #: Branches listed for the verification that step 1 did not list: nothing deleted from them.
    late: list[str] = Field(default_factory=list)


#: What every `dangling:<ref>@<n>` surface says first. Measured on pylance 12.0.0: when a branch's retained
#: versions reference only some of the files of the version it was cut from, the parent's cleanup keeps
#: that version's manifest and deletes the other files, so the version stays listed and reading it fails;
#: `cleanup_old_versions(versions=[n])` then removes nothing, so only what keeps it can release it.
_DANGLING_DETAIL = (
    "listed but unreadable: cleanup kept its manifest for a branch standing on some of its files and deleted "
    "the rest; no fragment that still reads holds the subject, and every other fragment's data files are gone"
)


def _dangling_detail(held: _Holders) -> str:
    """A `dangling:` surface's detail: why the version is listed, and what releases it."""
    keepers = [*(f"branch:{name}" for name in sorted(held.branches)), *(f"tag:{name}" for name in sorted(held.tags))]
    keepers += [f"history:{ref} (not reclaimed)" for ref in sorted(held.unreclaimed)]
    if held.window:
        keepers.append("the retention window")
    if held.unread or not keepers:
        keepers.append("a holder this could not read")
    return (
        f"{_DANGLING_DETAIL}. It stays listed, and a read of it fails, until what keeps it lets go: {keepers} "
        "(a branch by deletion or a rewrite of its head, a tag by deletion)"
    )


class _Verdict(StrEnum):
    ANSWERS = "answers"
    CLEAN = "clean"
    DANGLING = "dangling"
    UNREADABLE = "unreadable"


def _versions_still_matching(history: _History, refs: Sequence[str | None], predicate: str) -> _Verification:
    """Every retained version of every ref that still answers ``predicate`` — the erasure's own proof.

    ``history`` reads through a handle on main opened on a session nothing has read through, so every
    answer comes from storage. Asked of each ref's VERSIONS rather than of its head, because a head is
    the one surface the delete already reached. A version that cannot be read whole is probed fragment
    by fragment (:func:`_probe_fragments`); one that stays unproved is residual: unreadable is not
    evidence of absence, and this function exists to produce evidence.
    """
    found = _Verification()
    listing = _StorageListing(history.cold)
    for ref in refs:
        try:
            versions = sorted(history.versions(ref))
        except Exception as exc:  # noqa: BLE001
            log.warning("erasure_verify_list_failed", extra={"ref": _label(ref), "error": str(exc)})
            found.unlisted.append(_label(ref))
            continue
        for version in versions:
            name = _name((ref, version))
            verdict = _probe((ref, version), history.cold, predicate, listing)
            if verdict is _Verdict.DANGLING:
                found.dangling.append(name)
            elif verdict is not _Verdict.CLEAN:
                found.residual.append(name)
            if verdict is _Verdict.UNREADABLE:
                found.unreadable.append(name)
    return found


def _probe(reference: _Reference, cold: _Dataset, predicate: str, listing: _StorageListing) -> _Verdict:
    """Whether one version holds the subject, read whole first and fragment by fragment if that fails."""
    try:
        handle = cold.checkout_version(reference)
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_verify_failed", extra={"reference": _name(reference), "error": str(exc)})
        return _Verdict.UNREADABLE
    try:
        # COUNTED, not materialised. The question is "does this version still hold the subject", and
        # building the matching rows to read `.num_rows` sizes the PROOF by the erasure — paid once per
        # retained version, and worst exactly when the subject has the most rows.
        return _Verdict.ANSWERS if handle.count_rows(filter=predicate) else _Verdict.CLEAN
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_verify_failed", extra={"reference": _name(reference), "error": str(exc)})
    try:
        return _probe_fragments(handle, reference[1], predicate, listing)
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_verify_fragments_failed", extra={"reference": _name(reference), "error": str(exc)})
        return _Verdict.UNREADABLE


def _probe_fragments(handle: _Dataset, version: int, predicate: str, listing: _StorageListing) -> _Verdict:
    """A version that cannot be read whole, judged by its fragments.

    A fragment that reads is counted. One that does not holds nothing only when storage no longer has
    ANY of its data files: Lance's own `tracked_files()` says where each file of this version lives and
    `all_files()` what the store holds, so the verdict is an observation, not a path this code resolved.
    A fragment with any file present, or a file whose location cannot be compared with the listing, is
    unproved and makes the version UNREADABLE.
    """
    located = _data_file_locations(handle, version)
    lost = False
    for fragment in handle.get_fragments():
        try:
            if fragment.count_rows(filter=predicate):
                return _Verdict.ANSWERS
            continue
        except Exception as exc:  # noqa: BLE001
            log.info("erasure_verify_fragment_unreadable", extra={"version": version, "error": str(exc)})
        if not all(listing.gone(located.get(f"data/{data_file.path}")) for data_file in fragment.data_files()):
            return _Verdict.UNREADABLE
        lost = True
    return _Verdict.DANGLING if lost else _Verdict.CLEAN


def _data_file_locations(handle: _Dataset, version: int) -> dict[str, str | None]:
    """``version``'s data files, keyed by the path Lance records (``data/<file>``), as full locations.

    A path that appears under two bases is mapped to None: it cannot be matched to one fragment's file.
    """
    located: dict[str, str | None] = {}
    for row in handle.tracked_files(min_version=version).read_all().to_pylist():
        if row["version"] != version or row["type"] != "data file":
            continue
        location = f"{str(row['base_uri']).rstrip('/')}/{row['path']}"
        located[row["path"]] = location if located.get(row["path"], location) == location else None
    return located


class _StorageListing:
    """Every object under the table's root, listed once and only when a probe needs it."""

    def __init__(self, cold: _Dataset) -> None:
        self._cold = cold
        self._present: set[str] | None = None
        self._bases: set[str] = set()

    def gone(self, location: str | None) -> bool:
        """True only when ``location`` lies under the listed root and the listing does not hold it."""
        if self._present is None:
            rows = self._cold.all_files().read_all().to_pylist()
            self._bases = {str(row["base_uri"]).rstrip("/") for row in rows}
            self._present = {f"{str(row['base_uri']).rstrip('/')}/{row['path']}" for row in rows}
        if location is None or not any(location.startswith(f"{base}/") for base in self._bases):
            return False
        return location not in self._present


class _History:
    """Each ref's retained versions and each version's data files, read from the cold handle once."""

    def __init__(self, cold: _Dataset, cutoffs: Mapping[str | None, datetime]) -> None:
        self.cold, self._cutoffs = cold, cutoffs
        self._versions: dict[str | None, dict[int, datetime | None]] = {}
        self._files: dict[_Reference, frozenset[str]] = {}

    def reclaimed(self, ref: str | None) -> bool:
        """Whether ``ref``'s cleanup ran: when it did not, nothing else is needed to explain a survivor."""
        return ref in self._cutoffs

    def versions(self, ref: str | None) -> dict[int, datetime | None]:
        """``{version: commit instant}`` for ``ref``; raises when its versions cannot be listed."""
        if ref not in self._versions:
            self._versions[ref] = {int(entry["version"]): _instant(entry.get("timestamp")) for entry in _head(self.cold, ref).versions()}
        return self._versions[ref]

    def inside_window(self, reference: _Reference) -> bool:
        """Newer than the cutoff its ref's reclaim ran with, which it must have. An instant this cannot
        read counts as newer."""
        stamp = self.versions(reference[0]).get(reference[1])
        return stamp is None or stamp > self._cutoffs[reference[0]]

    def files(self, reference: _Reference) -> frozenset[str]:
        """The data file paths ``reference``'s manifest lists — readable after the files are gone."""
        if reference not in self._files:
            fragments = self.cold.checkout_version(reference).get_fragments()
            self._files[reference] = frozenset(data_file.path for fragment in fragments for data_file in fragment.data_files())
        return self._files[reference]


class _Holders(BaseModel):
    """What keeps one version listed after the erasure's reclaim."""

    #: Tags naming it, or naming a retained version that still references its files.
    tags: set[str] = Field(default_factory=set)
    #: Branches whose HEAD still references its files: no cleanup takes a head.
    branches: set[str] = Field(default_factory=set)
    #: It, or a retained version referencing its files, is newer than the retention cutoff.
    window: bool = False
    #: Refs whose cleanup did not run, on it or on a retained version referencing its files.
    unreclaimed: set[str] = Field(default_factory=set)
    #: Something on the walk could not be read, so the fields above may be short.
    unread: bool = False

    def absorb(self, other: _Holders) -> None:
        self.tags |= other.tags
        self.branches |= other.branches
        self.window |= other.window
        self.unreclaimed |= other.unreclaimed
        self.unread |= other.unread


def _holders(
    reference: _Reference, history: _History, forks: Mapping[str, _Reference], tags: Mapping[str, _Reference | None], seen: frozenset[_Reference] = frozenset()
) -> _Holders:
    """What keeps ``reference`` listed: the retention window, a tag, or a branch cut from it that still
    references its data files.

    The last is Lance's fork pin, measured on pylance 12.0.0: the parent's cleanup keeps the version a
    branch was cut from while a retained version of that branch references one of its files, and takes
    it once none does. A retained branch version is kept by the same three causes, so the walk recurses;
    only a branch's HEAD, which no cleanup takes, makes the branch itself the holder.

    A version whose ref was not reclaimed is kept by that alone, and nothing is attributed past it: what
    would pin it once a reclaim runs is the next erasure's to measure, and naming a tag or a branch here
    would ask for a deletion that cannot finish the erasure.
    """
    if not history.reclaimed(reference[0]):
        return _Holders(unreclaimed={_label(reference[0])})
    held = _Holders(tags={name for name, named in tags.items() if named == reference})
    try:
        held.window = history.inside_window(reference)
        base = history.files(reference)
    except Exception as exc:  # noqa: BLE001
        log.warning("erasure_holders_unread", extra={"reference": _name(reference), "error": str(exc)})
        held.unread = True
        return held
    for child in sorted(name for name, fork in forks.items() if fork == reference):
        try:
            retained = history.versions(child)
            standing = [step for step in retained if history.files((child, step)) & base]
        except Exception as exc:  # noqa: BLE001
            log.warning("erasure_holders_unread", extra={"reference": _name(reference), "branch": child, "error": str(exc)})
            held.unread = True
            continue
        head = max(retained, default=None)
        for step in standing:
            if step == head:
                held.branches.add(child)
            elif (child, step) not in seen:
                held.absorb(_holders((child, step), history, forks, tags, seen | {reference}))
    return held


class _Account(BaseModel):
    """What the report says keeps the residual, and so what finishing the erasure takes."""

    pins: list[Pin] = Field(default_factory=list)
    held_by_retention: list[str] = Field(default_factory=list)
    #: Residual heads: the delete did not reach that ref, and no cleanup takes a head.
    heads: list[str] = Field(default_factory=list)
    #: Residuals kept because a ref's reclaim did not run, and those refs (see their `history:` surfaces).
    unreclaimed: list[str] = Field(default_factory=list)
    unreclaimed_refs: set[str] = Field(default_factory=set)
    #: Residuals nothing this could read accounts for.
    unexplained: list[str] = Field(default_factory=list)
    #: Branches whose fork point could not be read, so a holder may be missing from the fields above.
    unforked: list[str] = Field(default_factory=list)


def _account(residual: Sequence[str], history: _History, forks: Mapping[str, _Reference], tags: Mapping[str, _Reference | None]) -> _Account:
    """``pinned_by`` and the rest of what keeps ``residual``, in an order Lance accepts.

    A TAG is named when it holds a residual, directly or through a retained branch version that still
    references its files. A BRANCH is named only when its head does — deleting one destroys someone's
    working ref ([[LH-178]]) — and then with every descendant and every tag on them, because
    `branches.delete` refuses a branch another branch is cut from ("Branch work is referenced by
    [("deeper", 3)] versions") or a tag names ("referenced by tags [("t", 2)]"), measured on pylance
    12.0.0: tags first, then branches deepest first.
    """
    account = _Account()
    named: set[str] = set()
    doomed: set[str] = set()
    for name in residual:
        reference = _parse(name)
        try:
            is_head = reference[1] == max(history.versions(reference[0]))
        except Exception:  # noqa: BLE001 — an unlistable ref is already reported by the verification
            is_head = False
        if is_head:
            account.heads.append(name)
            continue
        held = _holders(reference, history, forks, tags)
        named |= held.tags
        doomed |= held.branches
        if held.window:
            account.held_by_retention.append(name)
        if held.unreclaimed:
            account.unreclaimed.append(name)
            account.unreclaimed_refs |= held.unreclaimed
        if held.unread or not (held.window or held.tags or held.branches or held.unreclaimed):
            account.unexplained.append(name)
    doomed = _with_descendants(doomed, forks)
    tag_pins = [Pin(ref=f"tag:{tag}", holds=_name(ref)) for tag, ref in sorted(tags.items()) if ref is not None and (tag in named or ref[0] in doomed)]
    deepest_first = sorted(doomed, key=lambda branch: (-_depth(branch, forks), branch))
    account.pins = [*tag_pins, *(Pin(ref=f"branch:{branch}", holds=_name(forks[branch])) for branch in deepest_first)]
    return account


def _with_descendants(branches: set[str], forks: Mapping[str, _Reference]) -> set[str]:
    closed, frontier = set(branches), list(branches)
    while frontier:
        parent = frontier.pop()
        children = {name for name, fork in forks.items() if fork[0] == parent} - closed
        closed |= children
        frontier.extend(children)
    return closed


def _verify_detail(found: _Verification, account: _Account) -> str:
    """The failed verify surface's detail: what is left, and what finishing takes."""
    answering = [version for version in found.residual if version not in found.unreadable]
    parts = [f"{answering} still answer this predicate"] if answering else []
    if found.unreadable:
        parts.append(f"{found.unreadable} could not be read")
    if found.unlisted:
        parts.append(f"the versions of {found.unlisted} could not be listed")
    if found.relist_failed:
        parts.append("the branch list could not be read again, so a branch cut during the erasure was not verified")
    if found.late:
        parts.append(f"{found.late} appeared during the erasure, after its deletes: erase again")
    if account.pins:
        parts.append(f"delete {[pin.ref for pin in account.pins]} in that order, then erase again")
    if account.held_by_retention:
        parts.append(f"{account.held_by_retention} are kept by the retention window: erase again with retain_days=0, or once it has passed")
    if account.heads:
        parts.append(f"{account.heads} are heads the delete did not reach: erase again")
    if account.unreclaimed:
        parts.append(f"{account.unreclaimed} remain because the reclaim of {sorted(account.unreclaimed_refs)} did not run: see their history surfaces")
    if account.unexplained:
        parts.append(f"nothing this could read accounts for {account.unexplained}")
    if account.unforked:
        parts.append(f"the fork point of {account.unforked} could not be read, so a ref keeping these may be missing from pinned_by")
    return "; ".join(parts)


def _depth(name: str, forks: Mapping[str, _Reference | None]) -> int:
    """How many branches ``name`` descends from, so a child always sorts before its parent.

    Every named parent counts as a hop, even one whose own fork point could not be read — the walk
    stops there, and its children still sort before it. Bounded by the branch count: Lance's fork
    points form a tree, and a malformed ``_refs`` must not hang the erasure door.
    """
    depth, fork = 0, forks.get(name)
    while fork is not None and fork[0] is not None and depth <= len(forks):
        depth, fork = depth + 1, forks.get(fork[0])
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


def _parse(name: str) -> _Reference:
    """:func:`_name` inverted — exact, because no branch is named `main` or carries an `@`."""
    label, _, version = name.rpartition("@")
    return (None if label == MAIN_BRANCH else label, int(version))


def _instant(stamp: object) -> datetime | None:
    """A `versions()` timestamp as UTC. pylance 12.0.0 answers a NAIVE host-local datetime, which
    `astimezone` reads as local; anything else is unknown."""
    return stamp.astimezone(UTC) if isinstance(stamp, datetime) else None


def _referred(dataset: _Dataset, protected: BaseRefs | None) -> str | None:
    """Why this table's files may not be rewritten or reclaimed (#114), or None when they may."""
    try:
        refuse_a_referring_datasets_source(dataset, protected)
    except UnsupportedOperationError as exc:
        return str(exc)
    return None


def _compaction_refusal(handle: _Dataset, storage_options: StorageOptions) -> str | None:
    """Why the compact door's gate refuses rewriting ``handle``'s ref, or None — with ONE allowance.

    A branch sets flag 16 and names this table's root, and a branch of a branch its parent's
    `tree/<name>` too, as dataset-root bases its inherited files resolve through (measured on pylance
    12.0.0), so the gate as the door asks it refuses every branch. Those bases are the table's own
    history, which the rewrite copies from on purpose, so the gate weighs only the others: a base
    outside this table, and whether any data file resolves through one. A branch of a shallow clone
    names the clone's source as well (measured), and is refused with the clone.
    """
    reader, writer = manifest_feature_flags(handle)
    if not (reader | writer) & FLAG_BASE_PATHS:
        return describe_compaction_unsupported_flags(reader, writer, None)
    root = str(handle.uri)
    stored = dataset_root_probe(root, storage_options)
    # The table's own bases are dataset roots by construction and dropped below, so they cost no probe.
    gathered = gather_compaction_bases(handle, lambda path: _inside(path, root) or stored(path))
    try:
        through: bool | None = any(not _inside(path, root) for path in data_file_base_paths(handle))
    except Exception as exc:  # noqa: BLE001 — unread is the gate's refusal, never a permit
        log.warning("erasure_data_file_bases_unread", extra={"location": root, "error": str(exc)})
        through = None
    foreign = CompactionBases(bases=[base for base in gathered.bases if not _inside(base.path, root)], data_resolves_through_a_base=through)
    if not foreign.bases and through is False:
        return describe_compaction_unsupported_flags(reader & ~FLAG_BASE_PATHS, writer & ~FLAG_BASE_PATHS, None)
    return describe_compaction_unsupported_flags(reader, writer, foreign)


def _inside(path: str, root: str) -> bool:
    """Whether a base is this table's root or one of its branches' — compared as paths, not spellings."""
    base, table = normalise(path), normalise(root)
    return base == table or base.startswith(f"{table}/tree/")


def _compact(handle: _Dataset) -> str:
    """Rewrite ``handle``'s ref and say what it cost: the bytes of the data files the rewrite added,
    which on a branch are the inherited fragments it copied.

    THE SAME BOUND THE COMPACT DOOR CARRIES, imported rather than restated: this pod has more than one
    button that reaches `compact_files`, and an erasure runs over exactly the tables most likely to hold
    a blob column — so the unbounded default is not the cheaper path here.
    """
    before = _data_file_sizes(handle)
    metrics = handle.optimize.compact_files(**COMPACTION_BOUND)
    written = sum(size for path, size in _data_file_sizes(handle).items() if path not in before)
    removed, added = int(getattr(metrics, "fragments_removed", 0) or 0), int(getattr(metrics, "fragments_added", 0) or 0)
    return f"{removed} fragments rewritten into {added}, {written} bytes written"


def _data_file_sizes(handle: _Dataset) -> dict[str, int]:
    """Each data file of the version ``handle`` has open, by path. Measured on pylance 12.0.0,
    `compact_files` advances the handle it runs on and records each new file's on-disk size."""
    return {data_file.path: int(data_file.file_size_bytes or 0) for fragment in handle.get_fragments() for data_file in fragment.data_files()}


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
