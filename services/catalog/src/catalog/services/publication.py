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

import contextlib
from typing import TYPE_CHECKING

from lance_namespace import (
    CreateTableTagRequest,
    DeleteTableTagRequest,
    GetTableTagVersionRequest,
    InvalidInputError,
    InvalidTableStateError,
    TableTagNotFoundError,
    TableVersionNotFoundError,
    UpdateTableTagRequest,
)
from pydantic import BaseModel

from catalog.core.namespace import open_dataset
from catalog.services import dataplane
from service_kit.lakehouse.quality import Assertion, assert_quality, passed


if TYPE_CHECKING:
    from collections.abc import Sequence

    from lance_namespace import LanceNamespace


#: The pointer a consumer reads. One flat name per dataset — tag names cannot contain `/`, so a
#: branch cannot carry its own `published` and must encode the branch in the name if it ever needs
#: one. Matches `BLESSED_TAG` in the model registry: same concept, different subject.
PUBLISHED_TAG = "published"

#: Held on the candidate version for the DURATION OF THE GATE, then dropped.
#:
#: `cleanup_old_versions` exempts only TAGGED versions, and version N is untagged for exactly as long
#: as the gate takes to run — so a slow gate against a short `older_than` lets maintenance collect the
#: very version being gated, and the publish then advances a pointer at something that no longer
#: exists. The window is small and entirely real, and it widens with every assertion added to the
#: gate. A tag closes it: while `publishing` names N, maintenance cannot touch it.
#:
#: Removed in a `finally`, so a crashed gate leaves at most one stale `publishing` tag rather than
#: pinning a version forever. A stale one is visible in `ListTableTags` and blocks nothing except the
#: GC of one version.
PUBLISHING_TAG = "publishing"


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


def _tag_version(ns: LanceNamespace, so: dict[str, str], table_id: Sequence[str], tag: str) -> int | None:
    """The version a tag points at, or None when unset.

    Goes through the catalog's own `GetTableTagVersion` rather than reading `ds.tags` directly, so
    there is ONE resolution path and it is the one the spec defines. The dataplane already converts
    pylance's raise-on-unset (`Ref not found` — it does NOT return None) into the spec's
    `TableTagNotFound`; this turns that into the absence it represents.
    """
    try:
        return int(dataplane.get_tag_version(ns, so, GetTableTagVersionRequest(id=list(table_id), tag=tag)).version)
    except TableTagNotFoundError:
        return None


def published_version(ns: LanceNamespace, so: dict[str, str], table_id: Sequence[str]) -> int | None:
    """The version `published` points at, or None when nothing has been published yet."""
    return _tag_version(ns, so, table_id, PUBLISHED_TAG)


def _set_tag(ns: LanceNamespace, so: dict[str, str], table_id: Sequence[str], tag: str, version: int) -> None:
    """Create the tag, or move it if it already exists.

    The spec splits create and update into two operations and refuses the wrong one, so which to call
    depends on state the caller cannot assume — the first publication of every dataset needs create,
    every later one needs update.
    """
    if _tag_version(ns, so, table_id, tag) is None:
        dataplane.create_tag(ns, so, CreateTableTagRequest(id=list(table_id), tag=tag, version=version))
    else:
        dataplane.update_tag(ns, so, UpdateTableTagRequest(id=list(table_id), tag=tag, version=version))


def publish(
    ns: LanceNamespace,
    storage_options: dict[str, str],
    *,
    table_id: Sequence[str],
    version: int,
    key_column: str,
    required_columns: Sequence[str] = (),
    tag: str = PUBLISHED_TAG,
) -> PublicationResult:
    """Gate `version`, then advance `published` to it. Returns the range the notification should carry.

    Fail-closed in every direction: an out-of-range version raises before anything is read, and a
    failed assertion returns `published=False` with the tag untouched, so the previously published
    version keeps serving. The assertions travel back either way — a blocked batch has to be
    auditable, not merely rejected.

    The caller is already authorized; this decides only whether the DATA is good enough, which is the
    validator half of governance. FGA decides who MAY publish.
    """
    if version < 1:
        raise InvalidInputError(f"version must be >= 1, got {version}")

    table_id = list(table_id)
    dataset = open_dataset(ns, storage_options, table_id)
    latest = int(dataset.version)
    if version > latest:
        raise TableVersionNotFoundError(f"version {version} not found (latest is {latest})")

    previous = _tag_version(ns, storage_options, table_id, tag)
    if previous is not None and version < previous:
        # Publishing backwards is a rollback, and a rollback arriving by accident — a late retry of an
        # older run — silently un-publishes newer good data. Checked BEFORE the gate: there is no point
        # spending a full scan on a version that cannot be published either way.
        raise InvalidTableStateError(
            f"refusing to move {tag!r} backwards from {previous} to {version}; roll back explicitly via the tag API if that is intended"
        )

    try:
        # Pin the candidate for the gate's duration — see PUBLISHING_TAG.
        _set_tag(ns, storage_options, table_id, PUBLISHING_TAG, version)

        # Gate the version being published, not `latest`: they differ the moment another writer commits
        # while this gate runs, and publishing a version nobody checked is the whole failure this
        # prevents.
        candidate = open_dataset(ns, storage_options, table_id, version=version)
        assertions = assert_quality(
            candidate.uri,
            storage_options,
            key_column=key_column,
            required_columns=tuple(required_columns),
            # The pin, carried. Passing only `candidate.uri` dropped it: `assert_quality` re-opened
            # the dataset bare and scanned `latest`, so this gate answered for a version it was not
            # publishing — in both directions, and silently in the one that matters.
            version=version,
        )

        if not passed(assertions):
            failed = [a.assertion for a in assertions if not a.success]
            return PublicationResult(
                table=candidate.uri,
                published=False,
                from_version=previous,
                to_version=version,
                assertions=assertions,
                reason=f"quality gate failed: {', '.join(failed)}",
            )

        _set_tag(ns, storage_options, table_id, tag, version)
        return PublicationResult(
            table=candidate.uri,
            published=True,
            from_version=previous,
            to_version=version,
            assertions=assertions,
        )
    finally:
        # Suppressed on purpose: a failure to unpin must never mask the publish outcome, and the worst
        # case is one version exempt from GC until someone deletes a visible tag.
        with contextlib.suppress(Exception):
            dataplane.delete_tag(ns, storage_options, DeleteTableTagRequest(id=table_id, tag=PUBLISHING_TAG))
