"""Which of a dataset's versions its cleanup would reclaim, by version, in Lance's own words; and the refs that pin them.

``explain_cleanup_old_versions`` is ``cleanup_old_versions`` asked without deleting (pylance 13.0.0), so a preview built
on it names exactly what the run removes ([[LH-562]]). It counts versions and lists the files it would remove, each
version's manifest among them, named as ``lance_docs/lance/format/table/transaction.md`` (Manifest Naming Schemes)
spells it, so the versions are read off those names. Tags and child branches pin versions
(``lance_docs/lance/guide/tags_and_branches.md``): :func:`tag_versions` and :func:`fork_versions` name them for a door
that refuses to delete a pinned version.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol


if TYPE_CHECKING:
    from lance.lance import CleanupExplanation


#: The default ref. Lance records main as null — a tag's ``branch`` and a branch's ``parentBranch``
#: (``lance_docs/lance/format/table/branch_tag.md`` "Tag File Format" / "Branch Metadata File Format"). It is not in ``branches.list()`` on
#: any dataset, so a branch create's collision pre-check cannot see it, and pylance answers a request to create it with
#: its own bug-report text (measured on 11.0.0, 2026-09-15).
MAIN_BRANCH: Final = "main"


class VersionedDataset(Protocol):
    """The slice of a ``LanceDataset`` the rule reads."""

    @property
    def version(self) -> int: ...

    def versions(self) -> Sequence[Mapping[str, Any]]: ...

    @property
    def tags(self) -> Any: ...  # noqa: ANN401 — pylance's Tags, no protocol

    @property
    def branches(self) -> Any: ...  # noqa: ANN401 — pylance's Branches, no protocol


class ExplainedDataset(Protocol):
    """The slice of a ``LanceDataset`` :func:`reclaimable_versions` reads: its versions and its cleanup's explanation."""

    def versions(self) -> Sequence[Mapping[str, Any]]: ...

    def explain_cleanup_old_versions(
        self,
        older_than: timedelta | None = None,
        retain_versions: int | None = None,
        *,
        error_if_tagged_old_versions: bool = True,
        versions: list[int] | None = None,
        include_files: bool = False,
        max_files: int = 1000,
    ) -> CleanupExplanation: ...


def recorded_branch(branch: object) -> str | None:
    """A ref as Lance records it: ``None`` for main, the branch name otherwise.

    Lance stores a reference spelled ``("main", n)`` as null too (measured on pylance 12.0.0), while a request may
    name main by name, so comparing refs through this makes both spellings one ref.
    """
    if branch is None or branch == MAIN_BRANCH:
        return None
    if not isinstance(branch, str):
        raise TypeError(f"a branch is named by a str or None, got {type(branch).__name__}")
    return branch


def tag_versions(ds: VersionedDataset, branch: str | None) -> dict[str, int]:
    """``{tag: version}`` for the tags pinning a version OF ``branch`` — the only tags its cleanup honours.

    Branch histories are numbered independently (``lance_docs/lance/guide/tags_and_branches.md`` Branches), and measured on pylance 12.0.0 a tag
    on ``work`` v3 leaves main's v3 to ``cleanup_old_versions``.
    """
    ref = recorded_branch(branch)
    return {
        name: int(version)
        for name, tag in ds.tags.list().items()
        if recorded_branch(tag.get("branch")) == ref and isinstance(version := tag.get("version"), int)
    }


def fork_versions(ds: VersionedDataset, branch: str | None) -> dict[str, int]:
    """``{child branch: version}`` for the branches cut from ``branch`` — each pins the version it forked at.

    Measured on pylance 12.0.0 at ``older_than=0``: main keeps the version a branch was cut from and deletes its
    untagged neighbours. Only DIRECT children are read: a grandchild stands on the same parent version its ancestor does.
    """
    ref = recorded_branch(branch)
    return {
        name: int(version)
        for name, meta in ds.branches.list().items()
        if recorded_branch(meta.get("parent_branch")) == ref and isinstance(version := meta.get("parent_version"), int)
    }


#: The candidate files one explanation lists: a version's manifest is one of them, beside its data, transaction,
#: deletion and index files. Past it, Lance truncates the list and the versions are asked one at a time.
EXPLAIN_FILE_LIMIT: Final = 100_000

#: The highest version number, ``u64::MAX``: a V2 manifest is named ``{u64::MAX - version:020}.manifest``.
_U64_MAX: Final = 2**64 - 1


def manifest_version(path: str) -> int | None:
    """The version a manifest's file name names, V1 ``{version}.manifest`` or V2 ``{u64::MAX - version:020}.manifest``;
    ``None`` for any other name (a staged ``.manifest-<uuid>``, which no cleanup lists)."""
    stem, dot, extension = path.rsplit("/", 1)[-1].partition(".")
    if dot != "." or extension != "manifest" or not stem.isdigit():
        return None
    return _U64_MAX - int(stem) if len(stem) == 20 else int(stem)


def reclaimable_versions(ds: ExplainedDataset, *, older_than: timedelta, retain_versions: int | None) -> list[int]:
    """The versions ``cleanup_old_versions(older_than, retain_versions, error_if_tagged_old_versions=False)`` would
    remove from the ref ``ds`` is opened on, newest first: its explanation's manifests, or, when Lance truncated the
    list it gives, its explanation of each version alone."""
    explained = ds.explain_cleanup_old_versions(
        older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False, include_files=True, max_files=EXPLAIN_FILE_LIMIT
    )
    if not explained.candidate_files_truncated:
        named = (manifest_version(candidate.path) for candidate in explained.candidate_files if candidate.kind == "manifest")
        return sorted((version for version in named if version is not None), reverse=True)
    held = (int(v["version"]) for v in ds.versions())
    return sorted((version for version in held if _removes(ds, version, older_than=older_than, retain_versions=retain_versions)), reverse=True)


def _removes(ds: ExplainedDataset, version: int, *, older_than: timedelta, retain_versions: int | None) -> bool:
    """Whether the cleanup would remove ``version``, by its explanation of that version alone."""
    explained = ds.explain_cleanup_old_versions(older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False, versions=[version])
    return explained.stats.old_versions > 0
