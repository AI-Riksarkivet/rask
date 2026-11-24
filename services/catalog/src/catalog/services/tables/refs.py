"""A table's branches and tags: list, create, update and delete, and the rules a ref's name is held to."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Any, Final

import lance
from lance_namespace import (
    CreateTableBranchRequest,
    CreateTableBranchResponse,
    CreateTableTagRequest,
    CreateTableTagResponse,
    DeleteTableBranchRequest,
    DeleteTableTagRequest,
    DeleteTableTagResponse,
    GetTableTagVersionRequest,
    GetTableTagVersionResponse,
    InvalidInputError,
    InvalidTableStateError,
    ListTableBranchesRequest,
    ListTableBranchesResponse,
    ListTableTagsRequest,
    ListTableTagsResponse,
    ServiceUnavailableError,
    TableBranchAlreadyExistsError,
    TableBranchNotFoundError,
    TableTagNotFoundError,
    UpdateTableTagRequest,
    UpdateTableTagResponse,
)
from pydantic import BaseModel, ConfigDict

from catalog.core.lance_refusals import ref_errors
from catalog.core.pagination import paginate
from catalog.core.version_retention import MAIN_BRANCH, recorded_branch
from catalog.services import table_claims
from catalog.services.tables import hidden_branches
from catalog.services.tables.versions import BRANCH_IDENTIFIER_KEY, identifier_of
from lakehouse.lancekit import branch_layout
from lakehouse.records.history_fences import HistoryWork
from lakehouse.storage.options import StorageOptions


__all__ = [
    "BranchDeleteStoppedError",
    "RemovedBranch",
    "TagReference",
    "create_branch",
    "create_tag",
    "delete_branch",
    "delete_tag",
    "get_tag_version",
    "list_branches",
    "list_tags",
    "refuse_a_branch_name_that_nests_with_another",
    "refuse_a_branch_name_that_shadows_the_layout",
    "refuse_a_branch_name_the_backend_cannot_use",
    "tag_reference",
    "update_tag",
]


#: The longest object-store key segment, in bytes, a store is held to: measured on RustFS 1.0.3 (the development store),
#: a 255-byte segment is written and a 256-byte one refused.
_KEY_SEGMENT_BYTES: Final = 255


def refuse_a_branch_name_the_backend_cannot_use(name: str) -> None:
    """Refuse the branch names pylance does not refuse as the caller's mistake before it writes, with the spec's codes.

    [[LH-046]] LANCE OWNS THE GRAMMAR AND THIS IS NOT A SECOND COPY OF IT. `lance_docs/lance/format/table/branch_tag.md`
    § "Branch Name" is the grammar, and pylance validates a name against it before it writes anything: measured on 13.0.0
    over a `dir` table and a moto S3 one, an empty name, a leading, trailing or doubled ``/``, a ``..`` or ``\\`` anywhere,
    a character outside a segment's alphanumerics and ``.-_``, and a ``.lock`` suffix each answer ``Ref is invalid: …``
    with no object written, and `_classify_ref_error` maps that marker to InvalidInput. A copy of those rules here would be
    a second grammar, the one nobody re-tests against a new pylance.

    THREE KINDS OF NAME ARE NOT REFUSED THAT WAY ([[LH-283]]):

    * ``main`` — rule 7 reserves it for the default branch, and pylance answers it ``Ref is invalid``. The door answers
      the spec's 23 instead: every table has main, so "already exists" is the true statement.
    * a ``.`` segment — a path step, which the object store's path parser refuses and the grammar admits (rule 5 allows
      ``.`` in a segment; rule 4 forbids ``..``, which pylance refuses itself). pylance hands ``a/./b`` to the store and
      answers ``LanceError(IO): Error parsing Path … illegal character sequence "."``, which carries no code and would
      answer Internal 18.
    * a name whose ref file no key segment holds (`_refuse_a_ref_name_no_key_segment_holds`) — pylance writes the
      branch's ``tree/<name>/`` before its ref file, so the store's refusal comes after that write (measured on RustFS
      1.0.3 with pylance 13.0.0): the branch's manifest stays with no ref naming it, no door deletes it, and every later
      create of the name answers pylance's bug-report text.
    """
    if name == MAIN_BRANCH:
        raise TableBranchAlreadyExistsError(f"branch {name!r} always exists and cannot be created")
    if "." in name.split("/"):
        raise InvalidInputError(f"invalid branch name {name!r}: a '.' segment is a path step, not a name the store can hold")
    _refuse_a_ref_name_no_key_segment_holds("branch", name)


def _ref_file_bytes_past_a_key_segment(name: str) -> int | None:
    """The bytes the ref file of a branch or tag named ``name`` takes, when that is more than a key segment holds."""
    size = len(branch_layout.ref_file_name(name).encode())
    return size if size > _KEY_SEGMENT_BYTES else None


def _refuse_a_ref_name_no_key_segment_holds(kind: str, name: str) -> None:
    """Refuse, as the caller's mistake (code 13), a branch or tag name whose ref file no object-store key segment holds.

    A ref's file is its name URL-encoded plus ``.json`` under ``_refs/branches`` or ``_refs/tags``
    (`branch_layout.ref_file_name`; `lance_docs/lance/format/table/branch_tag.md` § "Branch Metadata Path" and
    § "Tag Storage"), so ``/`` and every non-ASCII byte cost three bytes. Measured on RustFS 1.0.3 with pylance 13.0.0,
    the store refuses a file past 255 bytes with ``Generic S3 error``, which carries no code and would answer Internal
    18. A local filesystem root stages the file as ``<file>#<n>``, so there a name a few bytes under the bound still
    fails; such a root is a test fixture.
    """
    if (size := _ref_file_bytes_past_a_key_segment(name)) is not None:
        raise InvalidInputError(
            f"invalid {kind} name {name!r}: its ref file, the name URL-encoded plus '.json', would be {size} bytes, longer than the "
            f"{_KEY_SEGMENT_BYTES} an object-store key segment holds"
        )


def _require_a_tag_a_key_segment_can_hold(tag: str) -> None:
    """A tag whose ref file no key segment holds cannot exist, so a lookup of one is TableTagNotFound (8) ([[LH-283]]).

    pylance looks a tag up by reading its file, and the store refuses that key with an error that carries no code
    (measured on RustFS 1.0.3 with pylance 13.0.0: a HEAD of a 256-byte segment), which would answer Internal 18.
    """
    if _ref_file_bytes_past_a_key_segment(tag) is not None:
        raise TableTagNotFoundError(f"tag {tag!r} not found: no tag's ref file can be longer than {_KEY_SEGMENT_BYTES} bytes")


def refuse_a_branch_name_that_shadows_the_layout(name: str) -> None:
    """Refuse a branch name with a segment Lance uses as a directory name ([[LH-203]]).

    Lance's grammar accepts ``_`` in a segment, so ``a/_versions`` is a valid branch name, and the format
    joins it verbatim onto ``tree/``: its files land in ``tree/a/_versions/``, which is branch ``a``'s own
    version directory. That makes it the estate's rule rather than Lance's, so it is refused here as the
    caller's input (13), whether or not a branch ``a`` exists yet: once one does, the two share files.
    """
    if (segment := branch_layout.reserved_segment(name)) is not None:
        raise InvalidInputError(
            f"invalid branch name {name!r}: the segment {segment!r} is a directory name in a Lance dataset's layout, so the branch's files would land "
            "inside another ref's"
        )


def refuse_a_branch_name_that_nests_with_another(name: str, branches: Iterable[str]) -> None:
    """Refuse a name whose directory would lie inside an existing branch's, or contain one (409, code 19).

    `a/b` lives at ``tree/a/b/``, inside ``tree/a/``. Lance then treats ``a/b``'s files as part of ``a``'s
    directory: a delete of ``a`` leaves its files behind, and a vend or reclaim for ``a`` reaches ``a/b``.
    The name is well formed, so this is the table's state refusing it, which is the spec's 19
    InvalidTableState rather than 13.
    """
    existing = list(branches)
    if clash := branch_layout.branches_enclosing(name, existing) or branch_layout.branches_inside(name, existing):
        raise InvalidTableStateError(
            f"branch {name!r} cannot be created: its directory would nest with branch {clash[0]!r}'s, and a branch's directory holds only its own "
            "files. Choose a name that is not a '/'-prefix of an existing branch and has none"
        )


def list_tags(table: table_claims.DoorTable, so: StorageOptions, req: ListTableTagsRequest) -> ListTableTagsResponse:
    """List the table's tags as ``{name: TagContents{version, manifest_size, branch}}``, paged by name (``req.limit``,
    ``req.page_token``): pylance lists every tag at once, so the page is cut here."""
    listed = table.open_unchecked(so).tags.list()
    page, next_token = paginate(sorted(listed), req.page_token, req.limit)
    tags: dict[str, dict[str, Any]] = {}
    for name, tag in ((name, listed[name]) for name in page):
        # pylance's Tag is a TypedDict (plain dict at runtime), so read by key.
        entry = tag if isinstance(tag, dict) else {"version": getattr(tag, "version", None)}
        tags[name] = {
            "version": entry.get("version"),
            "manifest_size": entry.get("manifest_size") or 0,
            "branch": entry.get("branch"),  # None for a tag on main (TagContents.branch is optional)
        }
    # model_validate coerces the inner dicts into TagContents (not exported to name directly).
    return ListTableTagsResponse.model_validate({"tags": tags, "page_token": next_token})


def _open_with_branch(table: table_claims.DoorTable, so: StorageOptions, branch: str | None) -> lance.LanceDataset:
    """Main's handle, once the ``branch`` a tag op names is one the table has ([[LH-406]]).

    pylance resolves ``(branch, version)`` as one reference and reports a missing branch as a missing version
    (``version ghost:1 does not exist``), so the branch is established by reading the table's branch list first.

    Raises:
        TableBranchNotFoundError: The table has no branch ``branch``.
    """
    dataset = table.open(so)
    if (named := recorded_branch(branch)) is not None and named not in dataset.branches.list():
        raise TableBranchNotFoundError(f"branch {named!r} not found")
    return dataset


def _tag_reference(branch: str | None, version: int | None) -> int | tuple[str | None, int | None] | None:
    """Map the spec's optional ``branch`` + ``version`` to a pylance tag ``reference``: a bare int resolves
    against the CURRENT branch (main), so a branch-scoped tag must pass the ``(branch, version)`` tuple."""
    return (branch, version) if branch is not None else version


def create_tag(table: table_claims.DoorTable, so: StorageOptions, req: CreateTableTagRequest) -> CreateTableTagResponse:
    """Tag the given table version with a name (honoring the request's optional ``branch``), never while a reclaim runs.

    Raises:
        InvalidInputError: The tag's ref file would be longer than an object-store key segment ([[LH-283]]).
        ConcurrentModificationError: A reclaim holds the table's history ([[LH-308]]); no tag was written.
    """
    _refuse_a_ref_name_no_key_segment_holds("tag", req.tag)
    dataset = _open_with_branch(table, so, req.branch)
    with table.history_held(HistoryWork.REFS), ref_errors("tag", req.tag):
        dataset.tags.create(req.tag, _tag_reference(recorded_branch(req.branch), req.version))
    return CreateTableTagResponse()


class TagReference(BaseModel):
    """What a tag names: a version within ONE branch's history.

    A tag file records ``branch`` beside ``version`` (`lance_docs/lance/format/table/branch_tag.md` § Tag File Format), and
    branch histories are numbered independently (`lance_docs/lance/guide/tags_and_branches.md` § Branches), so the version
    alone names another snapshot on main.
    """

    model_config = {"frozen": True}

    tag: str
    #: The branch the version lives on, ``None`` for main.
    branch: str | None
    version: int

    def __str__(self) -> str:
        return f"tag {self.tag!r} names version {self.version} of " + (f"branch {self.branch!r}" if self.branch is not None else "main")


def tag_reference(dataset: lance.LanceDataset, tag: str) -> TagReference:
    """The ref ``tag`` names on ``dataset``'s table, read with its branch: the one place a tag is resolved.

    pylance's ``tags.get_version`` answers the version alone (measured on pylance 13.0.0: a tag on ``("work", 3)``
    answers 3), so the ref is read off ``tags.list()``, whose entry carries both in one read. Tags live at the table's
    root whatever branch they name (`branch_tag.md` § Tag Storage), so a handle on any ref lists them all.

    Raises:
        TableTagNotFoundError: No tag has the name, or none can: its ref file would be longer than a key segment.
    """
    _require_a_tag_a_key_segment_can_hold(tag)
    entry = dataset.tags.list().get(tag)
    if entry is None:
        raise TableTagNotFoundError(f"tag {tag!r} not found")
    return TagReference(tag=tag, branch=recorded_branch(entry.get("branch")), version=entry["version"])


def get_tag_version(table: table_claims.DoorTable, so: StorageOptions, req: GetTableTagVersionRequest) -> GetTableTagVersionResponse:
    """Return the ref a tag names: its version and, for a tag on a branch, that branch (404 on an unknown tag)."""
    ref = tag_reference(table.open_unchecked(so), req.tag)
    return GetTableTagVersionResponse(version=ref.version, branch=ref.branch)


def update_tag(table: table_claims.DoorTable, so: StorageOptions, req: UpdateTableTagRequest) -> UpdateTableTagResponse:
    """Move an existing tag to a new table version (honoring the request's optional ``branch``), never while a reclaim runs.

    Raises:
        TableTagNotFoundError: No tag can have the name: its ref file would be longer than a key segment ([[LH-283]]).
        ConcurrentModificationError: A reclaim holds the table's history ([[LH-308]]); the tag was not moved.
    """
    _require_a_tag_a_key_segment_can_hold(req.tag)
    dataset = _open_with_branch(table, so, req.branch)
    with table.history_held(HistoryWork.REFS), ref_errors("tag", req.tag):
        dataset.tags.update(req.tag, _tag_reference(recorded_branch(req.branch), req.version))
    return UpdateTableTagResponse()


def delete_tag(table: table_claims.DoorTable, so: StorageOptions, req: DeleteTableTagRequest) -> DeleteTableTagResponse:
    """Delete a tag from the table."""
    _require_a_tag_a_key_segment_can_hold(req.tag)
    with ref_errors("tag", req.tag):
        table.open_unchecked(so).tags.delete(req.tag)
    return DeleteTableTagResponse()


def _branch_reference(req: CreateTableBranchRequest) -> int | tuple[str | None, int | None] | None:
    """Map the spec's ``from_branch`` / ``from_version`` to pylance ``create_branch``'s ``reference``.

    fromBranch + fromVersion → ``(branch, version)``; fromBranch only → ``(branch, None)`` (latest of that
    branch); fromVersion only → the ``version`` int (on main); neither → ``None`` (latest of main).
    """
    if req.from_branch is not None:
        return (req.from_branch, req.from_version)
    if req.from_version is not None:
        return req.from_version
    return None


def list_branches(table: table_claims.DoorTable, so: StorageOptions, req: ListTableBranchesRequest) -> ListTableBranchesResponse:
    """List the table's branches as ``{name: BranchContents}``, read from pylance ``ds.branches`` and paged by name.

    The native ``DirectoryNamespace`` answers Unsupported to branch ops, but ``lance.LanceDataset`` implements them, so we
    back them in-process here exactly like tags. A ``Branch`` is a TypedDict (plain dict at runtime).
    """
    listed = table.open_unchecked(so).branches.list()
    page, next_token = paginate(sorted(listed), req.page_token, req.limit)
    branches: dict[str, dict[str, Any]] = {}
    for name, branch in ((name, listed[name]) for name in page):
        entry = branch if isinstance(branch, dict) else {}
        metadata = dict(entry.get("metadata") or {})
        # WHICH INCARNATION, in the one free-form map `BranchContents` has: the spec declares no field for
        # it, and a recreated branch restarts its numbering, so a reader holding `(branch, N)` from a
        # lineage event needs this to tell whether that N is still the same commit. Written over a user
        # key of the same name, because the value is Lance's record and not the user's.
        if (identifier := identifier_of(entry)) is not None:
            metadata[BRANCH_IDENTIFIER_KEY] = identifier
        branches[name] = {
            "parent_branch": entry.get("parent_branch"),
            "parent_version": entry.get("parent_version"),
            "create_at": entry.get("create_at"),
            "manifest_size": entry.get("manifest_size") or 0,
            "metadata": metadata,
        }
    return ListTableBranchesResponse.model_validate({"branches": branches, "page_token": next_token})


def create_branch(table: table_claims.DoorTable, so: StorageOptions, req: CreateTableBranchRequest) -> CreateTableBranchResponse:
    """Create a branch from main (or a source branch/version) — maps to pylance ``create_branch``.

    A COLLISION IS ESTABLISHED BY READING, NOT BY MATCHING A MESSAGE, and this is the one ref failure
    where that distinction is forced. pylance answers an existing branch with its own bug-report text
    — ``OSError("Encountered internal error. Please file a bug report ... Clone operation should not
    enter build_manifest.")`` — which is neither a stable discriminator nor an honest thing to pattern
    match: the day upstream fixes that panic, a matcher keyed on it silently reports Internal again.
    So the door reads the branch list, and confirms by RE-READING when the create still fails.
    """
    # Names the backend cannot use are refused BEFORE the listing read: `main` is absent from
    # `branches.list()` (the default ref is implicit), so the collision check below cannot see it.
    refuse_a_branch_name_the_backend_cannot_use(req.name)
    refuse_a_branch_name_that_shadows_the_layout(req.name)
    # `main` is a ref every table has and no branch list names (`lance_docs/lance/format/table/branch_tag.md` "Branch Name"),
    # so a source spelled `main` is main itself.
    req.from_branch = recorded_branch(req.from_branch)
    # The residual version failure belongs to the SOURCE, so name that — not the branch being created,
    # which exists nowhere yet and tells the caller nothing about what was missing.
    source = req.from_branch if req.from_branch is not None else MAIN_BRANCH
    # JUDGED INSIDE THE TABLE'S BRANCH-CREATE HOLD ([[LH-413]]): `a` and `a/b` are two ref files no write on a ref makes
    # collide, so two creates of nesting names each passed a listing the other had not yet changed. The hold excludes
    # every other branch create on the table, so the listing judged here is the one this create commits against.
    with table.history_held(HistoryWork.BRANCH):
        dataset = table.open(so)
        branches = dataset.branches.list()
        if req.name in branches:
            raise TableBranchAlreadyExistsError(f"branch {req.name!r} already exists")
        # A hidden branch's directory still holds its commits, which a branch created over it would read as its own and
        # one created inside or around it would nest with, as a listed one's would.
        kept = {hidden.name: hidden for hidden in table.hidden_branches()} if table.resolved.uuid is not None else {}
        if req.name in kept:
            raise InvalidTableStateError(
                f"a deleted branch {req.name!r} is kept until {kept[req.name].expires_at}: restore it at branches/restore, or create the name after that"
            )
        refuse_a_branch_name_that_nests_with_another(req.name, [*branches, *kept])
        # THE SOURCE IS ESTABLISHED BY READING TOO, and here the message leaves no choice: pylance renders
        # a missing source BRANCH and a missing source VERSION with the same object-store text, so a
        # classifier reading it alone answers 11 for a branch that does not exist — the wrong code, and a
        # caller sent hunting a version when the branch is what is absent. With no `from_version` at all it
        # renders differently again and matches nothing, falling through as Internal 18.
        if req.from_branch is not None and req.from_branch not in branches:
            raise TableBranchNotFoundError(f"source branch {req.from_branch!r} not found")
        try:
            with ref_errors("branch", source, invalid_name=req.name):
                dataset.create_branch(req.name, _branch_reference(req))
        except OSError:
            if req.name in table.open(so).branches.list():
                raise TableBranchAlreadyExistsError(f"branch {req.name!r} already exists") from None
            raise
    return CreateTableBranchResponse()


class RemovedBranch(BaseModel):
    """One branch a delete removed: its name, and the incarnation it was, read before anything was deleted."""

    model_config = ConfigDict(frozen=True)

    name: str
    identifier: str | None


class BranchDeleteStoppedError(InvalidTableStateError):
    """A branch delete stopped part-way: a pin made since its check refused one of the branches it was removing."""

    def __init__(self, message: str, removed: Sequence[RemovedBranch]) -> None:
        super().__init__(message)
        #: What it removed before it stopped, which is gone whatever the request answers.
        self.removed = list(removed)


class BranchHideStoppedError(ServiceUnavailableError):
    """A recoverable branch delete stopped part-way: the store refused a step for one of the branches it was hiding."""

    def __init__(self, message: str, removed: Sequence[RemovedBranch]) -> None:
        super().__init__(message)
        #: What it hid before it stopped, which is hidden whatever the request answers.
        self.removed = list(removed)


def delete_branch(
    table: table_claims.DoorTable, so: StorageOptions, req: DeleteTableBranchRequest, *, hiding: Callable[[], hidden_branches.Hiding | None] | None = None
) -> list[RemovedBranch]:
    """Delete a branch from the table, and every branch whose directory lies inside its own ([[LH-203]]); every branch
    it removed, the nested ones first, each with its identifier (so the door announces each, [[LH-413]]).

    THROUGH THE NESTED BRANCHES FIRST. Lance keeps a directory another branch lives in, so deleting
    ``a`` while ``a/b`` exists removed only ``a``'s ref and left ``tree/a``'s manifests, transactions and
    data behind for nothing to reclaim (measured on pylance 12.0.0). Deleting deepest first and ``a``
    last lets each delete remove its whole directory. The create door refuses a name that nests, so
    only a table that predates that refusal can carry such a pair.

    A branch that lies inside ANOTHER branch's files is refused instead (409): Lance removes the
    branch's directory recursively, so deleting ``a/_versions`` deletes ``tree/a/_versions``, which is
    ``a``'s whole history, and ``a`` stops opening. Deleting ``a`` removes both.

    WITH ``hiding``, where the table's warehouse may keep what is dropped for a while ([[LH-429]]), the delete runs inside
    the table's branch hold, which excludes a branch create, a restore, another such delete and every reclaim, and asks
    ``hiding`` there. Where it answers a deadline nothing is destroyed: each of those branches is hidden under the same
    refusals (:func:`hidden_branches.hide`) and :func:`restore_branch` brings them back whole. Where it answers ``None``
    (an erasure of the table is in flight) they are destroyed.

    Raises:
        ConcurrentModificationError: With ``hiding``, other work holds the table's history.
    """
    if hiding is None:
        return _delete_branch(table, so, req, hiding=None)
    with table.history_held(HistoryWork.BRANCH):
        return _delete_branch(table, so, req, hiding=hiding())


def _delete_branch(
    table: table_claims.DoorTable, so: StorageOptions, req: DeleteTableBranchRequest, *, hiding: hidden_branches.Hiding | None
) -> list[RemovedBranch]:
    dataset = table.open_unchecked(so)
    listed = dataset.branches.list()
    branches = list(listed)
    # Read before anything is deleted: a missing `a` with an `a/b` would otherwise delete `a/b` and then fail.
    if req.name not in branches:
        raise TableBranchNotFoundError(f"branch {req.name!r} not found")
    holders = [parent for parent in branch_layout.branches_enclosing(req.name, branches) if branch_layout.lies_in_files_of(req.name, parent)]
    if holders:
        raise InvalidTableStateError(
            f"branch {req.name!r} lies inside the files of branch {holders[0]!r}: deleting it would delete {holders[0]!r}'s files too. "
            f"Delete {holders[0]!r}, which deletes {req.name!r} with it. Nothing was deleted"
        )
    doomed = [*branch_layout.branches_inside(req.name, branches), req.name]
    # A BRANCH CUT FROM ONE OF THEM pins it: pylance refuses to delete a branch another is forked from
    # ("Ref conflict error: Branch a is referenced by [("c", 1)]", measured on 12.0.0). Asked before the
    # first delete, so the cascade never removes `a/b` and then stops at `a`.
    forks = sorted(child for child, meta in listed.items() if child not in doomed and (meta or {}).get("parent_branch") in doomed)
    if forks:
        raise _forked_from(req.name, forks)
    # A TAG NAMING A VERSION ON ONE OF THEM pins it the same way ("Ref conflict error: Branch b is referenced by
    # tags [("t", 1)]", measured on pylance 13.0.0), so it is asked before the first delete too.
    pins = sorted(tag for tag, meta in dataset.tags.list().items() if (meta or {}).get("branch") in doomed)
    if kept := [hidden for hidden in (table.hidden_branches() if pins else []) if hidden.tag in pins]:
        # The catalog's own tag, which no caller can move: it holds the version a deleted branch was cut from.
        raise InvalidTableStateError(
            f"branch {req.name!r} cannot be deleted: the deleted branch {kept[0].name!r} was cut from it or from a branch inside it and is kept "
            f"until {kept[0].expires_at}. Restore {kept[0].name!r} at branches/restore, or delete {req.name!r} after that. Nothing was deleted"
        )
    if pins:
        raise InvalidTableStateError(
            f"branch {req.name!r} cannot be deleted: tag {', '.join(repr(t) for t in pins)} names a version on it or on a branch inside it, and a "
            "tag pins its branch. Delete or move the tag first. Nothing was deleted"
        )
    if hiding is not None:
        return _hide_each(table, so, asked=req.name, doomed={name: listed[name] for name in doomed}, hiding=hiding)
    removed: list[RemovedBranch] = []
    for name in doomed:
        try:
            with ref_errors("branch", name):
                dataset.branches.delete(name)
        except TableBranchAlreadyExistsError as exc:
            # The ref-conflict marker here is a pin made after the read above, not a name collision.
            raise BranchDeleteStoppedError(
                f"deleting branch {req.name!r} stopped at {name!r}: a branch forked from it or a tag naming it since the check pins it. "
                f"Deleted before stopping: {[branch.name for branch in removed]}",
                removed,
            ) from exc
        removed.append(RemovedBranch(name=name, identifier=identifier_of(listed[name])))
    return removed


def _hide_each(table: table_claims.DoorTable, so: StorageOptions, *, asked: str, doomed: dict[str, Any], hiding: hidden_branches.Hiding) -> list[RemovedBranch]:
    """Hide each of ``doomed`` (name to its listing, the innermost first) for the delete of ``asked``; what it hid."""
    removed: list[RemovedBranch] = []
    for name, listed in doomed.items():
        try:
            hidden_branches.hide(table, so, name=name, listed=listed, hiding=hiding)
        except (OSError, ValueError, ServiceUnavailableError) as exc:
            # Lance refuses a tag with a ValueError; the store, with an OSError.
            raise BranchHideStoppedError(
                f"deleting branch {asked!r} stopped at {name!r}, which still stands: {exc}. Hidden before stopping: "
                f"{[branch.name for branch in removed]}. Delete it again",
                removed,
            ) from exc
        removed.append(RemovedBranch(name=name, identifier=identifier_of(listed)))
    return removed


def restore_branch(table: table_claims.DoorTable, so: StorageOptions, name: str, *, now: datetime) -> list[RemovedBranch]:
    """Bring back the hidden branch ``name`` with every commit made on it, and each hidden branch whose directory lies
    inside its own ([[LH-429]]); each branch restored, the outermost first.

    Raises:
        TableBranchNotFoundError: No recoverable delete keeps a branch of that name.
        InvalidTableStateError: Its deadline has passed, so it is being deleted for good.
        ConcurrentModificationError: A reclaim or another branch create holds the table's history.
    """
    restored: list[RemovedBranch] = []
    with table.history_held(HistoryWork.BRANCH):
        # Read inside the hold, which excludes the delete for good: what is found here is still whole.
        kept = {hidden.name: hidden for hidden in table.hidden_branches()}
        if name not in kept:
            raise TableBranchNotFoundError(f"no deleted branch {name!r} is kept for this table")
        if kept[name].past_deadline(now) or kept[name].purging:
            raise InvalidTableStateError(f"branch {name!r} was kept until {kept[name].expires_at} and is being deleted for good")
        for inner in [name, *sorted(branch_layout.branches_inside(name, list(kept)), key=len)]:
            hidden_branches.restore(table, so, kept[inner])
            restored.append(RemovedBranch(name=inner, identifier=kept[inner].identifier))
    return restored


def _forked_from(name: str, forks: Sequence[str]) -> InvalidTableStateError:
    return InvalidTableStateError(
        f"branch {name!r} cannot be deleted: {', '.join(repr(f) for f in forks)} is forked from it or from a branch inside it, and a fork "
        "pins its parent. Delete the fork first. Nothing was deleted"
    )
