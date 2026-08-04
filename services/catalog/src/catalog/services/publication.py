"""Publication — a committed version becomes CONSUMABLE only when the gate passes it.

`open_ingest.md` § D2, ruled 2026-08-04.

**A commit is not a publication (D-R1).** Before this, a writer's commit was instantly visible to
every reader: whatever landed was consumable, a quality gate that ran afterwards could not un-publish
it, and there was no way to hold consumers at the last good version while a bad one sat above it. So
the writer commits version N, the gate reads N, and only then does the pointer move. A gate FAILURE
leaves the pointer at N-1 — version N stays committed and unreferenced, auditable, consumed by
nobody.

The gate runs AFTER the commit on purpose. The long-standing objection was that a gate must run
*before* the version exists or bad rows are readable; the answer is that "readable" and "published"
are different things once a pointer exists. And post-commit is the only position that works at all:
Lance's change-data-feed diffs two versions, so a pre-commit gate has nothing to diff.

**The tag is the truth; the event is only a notification (D-R2).** `published` answers "what is
ready?" durably, so a consumer that was down for a week can simply ask. An event is a wake-up to
avoid polling, and one missed while a consumer was offline costs nothing.

**This lives in the CATALOG, not in any writer.** The ingest plane, a Ray job, a backfill script and
a person with catalog credentials must all publish the same way, or each reimplements the contract
and they drift. It is also the only correct home mechanically: a tag file carries no format-level
CAS, while the namespace spec's `UpdateTableTag` does return `ConcurrentModification`, so the
catalog is the one door where a concurrent advance can be detected.

Deliberately shaped after `services/models.py::promote`, which has run this exact pattern for the
model registry since it was written — gate first, fail-closed, then create-or-update the tag. Models
gate on metric thresholds and data gates on assertions, but the control flow is the same one, and
having two spellings of it would be the drift this module exists to prevent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import lance
from lance_namespace import InvalidInputError, InvalidTableStateError, TableVersionNotFoundError
from pydantic import BaseModel

from service_kit.lakehouse.quality import Assertion, assert_quality, passed


if TYPE_CHECKING:
    from collections.abc import Sequence


#: The pointer a consumer reads. One flat name per dataset — tag names cannot contain `/`, so a
#: branch cannot carry its own `published` and must encode the branch in the name if it ever needs
#: one. Matches `BLESSED_TAG` in the model registry: same concept, different subject.
PUBLISHED_TAG = "published"


class PublicationResult(BaseModel):
    """What a publish attempt did, and to what.

    `from_version`/`to_version` are the RANGE (D-R3) the notification carries, so a consumer resolves
    an exact row delta with `_row_created_at_version > from AND <= to` and keeps no bookmark of its
    own. `from_version` is None the first time a dataset is published — there is no prior published
    version, so the delta is "everything up to `to_version`".
    """

    table: str
    published: bool
    from_version: int | None
    to_version: int
    assertions: list[Assertion] = []
    reason: str | None = None


def published_version(uri: str, storage_options: dict[str, str]) -> int | None:
    """The version `published` points at, or None when nothing has been published yet."""
    return _tag_version(lance.dataset(uri, storage_options=storage_options), PUBLISHED_TAG)


def _tag_version(dataset: lance.LanceDataset, tag: str) -> int | None:
    """The version a tag points at, or None when unset.

    An unset tag RAISES (`Ref not found`) rather than returning None — the same pylance behaviour the
    model registry documents at `models.py:104-105`. Reading it as a falsy return means the first
    publication of every dataset crashes.
    """
    try:
        version = dataset.tags.get_version(tag)
    except ValueError:
        return None
    return int(version) if version is not None else None


def publish(
    uri: str,
    storage_options: dict[str, str],
    *,
    version: int,
    key_column: str,
    required_columns: Sequence[str] = (),
    tag: str = PUBLISHED_TAG,
) -> PublicationResult:
    """Gate `version`, then advance `published` to it. Returns the range the notification should carry.

    Fail-closed in every direction: an out-of-range version raises before anything is read, and a
    failed assertion returns `published=False` with the tag untouched, so the previous published
    version keeps serving. The assertions travel back either way — a blocked batch has to be
    auditable, not merely rejected.

    The caller is already authorized; this decides only whether the DATA is good enough, which is the
    validator half of governance. FGA decides who MAY publish.
    """
    if version < 1:
        raise InvalidInputError(f"version must be >= 1, got {version}")

    dataset = lance.dataset(uri, storage_options=storage_options)
    latest = int(dataset.version)
    if version > latest:
        raise TableVersionNotFoundError(f"version {version} not found (latest is {latest})")

    previous = _tag_version(dataset, tag)

    # Gate the version being published, not `latest` — they differ the moment a second writer commits
    # while this gate runs, and publishing a version nobody checked is the whole failure being
    # prevented here.
    at_version = lance.dataset(uri, storage_options=storage_options, version=version)
    assertions = assert_quality(
        at_version.uri,
        storage_options,
        key_column=key_column,
        required_columns=tuple(required_columns),
    )

    if not passed(assertions):
        failed = [a.assertion for a in assertions if not a.success]
        return PublicationResult(
            table=uri,
            published=False,
            from_version=previous,
            to_version=version,
            assertions=assertions,
            reason=f"quality gate failed: {', '.join(failed)}",
        )

    if previous is not None and version < previous:
        # Publishing backwards is a rollback, and a rollback that arrives by accident — a late
        # retry of an older run — silently un-publishes newer good data. An intentional rollback
        # goes through the catalog's tag API with the operator naming the version.
        raise InvalidTableStateError(
            f"refusing to move {tag!r} backwards from {previous} to {version}; roll back explicitly via the tag API if that is intended"
        )

    tags = dataset.tags
    if previous is None:
        tags.create(tag, version)
    else:
        tags.update(tag, version)

    return PublicationResult(
        table=uri,
        published=True,
        from_version=previous,
        to_version=version,
        assertions=assertions,
    )
