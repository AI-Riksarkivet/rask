"""Which of a dataset's versions a retention would reclaim: the one eligibility rule a reclaimer and its preview share.

A version is reclaimable when it is not the current version, no tag on its ref and no branch cut from its ref pins it,
it lies outside the retain-last-N window, and it is older than the age cutoff. The catalog's GC preview and
maintenance's expiry threshold ask the same question, so they read the same answer here.

The rule mirrors what ``cleanup_old_versions(older_than, retain_versions, error_if_tagged_old_versions=False)``
deletes, on the evidence of ``lance_docs/guide.md`` (Branches: "Lance ensures that cleanup does not delete files still
referenced by any branch"; tags exempt a version from cleanup).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from pydantic import BaseModel


#: The default ref. Lance records main as null — a tag's ``branch`` and a branch's ``parentBranch``
#: (``lance_docs/file_format.md`` "Tag File Format" / "Branch Metadata File Format"). It is not in ``branches.list()`` on
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


class Retention(BaseModel):
    """How much history a policy keeps: ``older_than`` an age bound, ``retain_versions`` a count; ``None`` is no bound."""

    older_than: timedelta | None = None
    retain_versions: int | None = None


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

    Branch histories are numbered independently (``lance_docs/guide.md`` Branches), and measured on pylance 12.0.0 a tag
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


def _as_utc(ts: object) -> datetime:
    """A version timestamp as an aware UTC datetime; an unknown shape reads as now, so its version is never eligible.

    A NAIVE datetime from ``ds.versions()`` is host-LOCAL wall-clock (pylance builds it with
    ``datetime.fromtimestamp(ns/1e9)``), so it is ``astimezone(UTC)``, never relabelled as UTC.
    """
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.astimezone(UTC)
    return datetime.now(UTC)


def _old_enough(version: Mapping[str, Any], cutoff: datetime | None) -> bool:
    """Whether ``version`` is past the age cutoff; with no cutoff, or no timestamp to read, every version is."""
    timestamp = version.get("timestamp")
    return cutoff is None or timestamp is None or _as_utc(timestamp) <= cutoff


def reclaimable_versions(ds: VersionedDataset, *, branch: str | None, retention: Retention, now: datetime) -> list[int]:
    """The versions ``retention`` would reclaim on ``branch``, newest first."""
    versions = sorted(ds.versions(), key=lambda v: int(v["version"]), reverse=True)
    kept = {int(ds.version), *tag_versions(ds, branch).values(), *fork_versions(ds, branch).values()}
    if retention.retain_versions:
        kept |= {int(v["version"]) for v in versions[: retention.retain_versions]}
    cutoff = now - retention.older_than if retention.older_than is not None else None
    return [int(v["version"]) for v in versions if int(v["version"]) not in kept and _old_enough(v, cutoff)]
