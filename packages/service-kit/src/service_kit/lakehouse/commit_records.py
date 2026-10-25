"""What the catalog keeps beside a table's version log so that every committed version is announced ([[LH-497]]).

Two control-root registries, both keyed by the table's uuid, so a rename moves neither and a table created later under
the name inherits neither (ADR 0173 §8):

- **The author of a stock commit that names none** (``_commit_authors/<table uuid>/<transaction uuid>.json``).
  ``version/create`` records the verified caller here before it finalizes a commit whose transaction properties carry
  no ``rask.author``: a stock compaction cannot carry one (pylance 13.0.0 commits ``ReserveFragments`` and ``Rewrite``
  with no properties, and models no class for the first), so the catalog's own record is how the version names who
  made it. Keyed by the transaction's uuid, never the version number, because a commit that loses its race leaves its
  record for a version another writer then holds.
- **The reconciler's mark** (``_commit_marks/table-<digest>.json``): for each ref of the table, the highest version up
  to which every version is known announced, so a pass reads only the versions after it.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from typing import Final

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict, ValidationError

from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base
from service_kit.lakehouse.record_store import delete_record, get_record, put_record, record_key


AUTHORS_PREFIX: Final = "_commit_authors"
MARKS_PREFIX: Final = "_commit_marks"


class CommitAuthor(BaseModel):
    """Who made one stock commit, as the catalog verified the caller of its ``version/create``."""

    model_config = ConfigDict(frozen=True)

    author: str
    transaction_uuid: str


def _author_key(table_uuid: str, transaction_uuid: str) -> str:
    return f"{AUTHORS_PREFIX}/{table_uuid}/{transaction_uuid}.json"


def record_author(control_root: str, storage_options: StorageOptions, table_uuid: str, transaction_uuid: str, author: str) -> None:
    """Record ``author`` as the maker of the commit whose transaction is ``transaction_uuid``, on the table ``table_uuid``."""
    record = CommitAuthor(author=author, transaction_uuid=transaction_uuid)
    put_record(control_root, storage_options, _author_key(table_uuid, transaction_uuid), record.model_dump())


def recorded_author(control_root: str, storage_options: StorageOptions, table_uuid: str, transaction_uuid: str) -> str | None:
    """The author recorded for the commit whose transaction is ``transaction_uuid``, or ``None`` when none is."""
    loaded = get_record(control_root, storage_options, _author_key(table_uuid, transaction_uuid), event="commit_author")
    if loaded is None:
        return None
    try:
        return CommitAuthor.model_validate(loaded).author
    except ValidationError:
        return None


def forget_author(control_root: str, storage_options: StorageOptions, table_uuid: str, transaction_uuid: str) -> None:
    """Delete the author record of one commit, once its version's announcement is on the feed."""
    delete_record(control_root, storage_options, _author_key(table_uuid, transaction_uuid))


def end_authors(control_root: str, storage_options: StorageOptions, table_uuid: str) -> bool:
    """Delete every author record of the table ``table_uuid``; whether any existed."""
    fs, base = fs_and_base(control_root, storage_options)
    directory = f"{base}/{AUTHORS_PREFIX}/{table_uuid}"
    if fs.get_file_info(directory).type == pafs.FileType.NotFound:
        return False
    fs.delete_dir(directory)
    return True


class CommitMarks(BaseModel):
    """For each ref of one table, the highest version up to which every version's announcement is known recorded.

    A ref is keyed by its name and its incarnation, ``""`` for main: a deleted and recreated branch restarts its
    numbering, so a mark kept for its earlier incarnation says nothing about the new one.
    """

    model_config = ConfigDict(frozen=True)

    refs: dict[str, int] = {}


def ref_key(branch: str | None, branch_identifier: str | None) -> str:
    """The key a ref's mark is kept under: ``""`` for main, else ``<branch>@<incarnation>``."""
    return "" if branch is None else f"{branch}@{branch_identifier or ''}"


def read_marks(control_root: str, storage_options: StorageOptions, table_uuid: str) -> CommitMarks:
    """The table's marks; none when it has no record, or one that does not read as marks."""
    loaded = get_record(control_root, storage_options, record_key(MARKS_PREFIX, "table", table_uuid), event="commit_marks")
    if loaded is None:
        return CommitMarks()
    try:
        return CommitMarks.model_validate(loaded)
    except ValidationError:
        return CommitMarks()


def write_marks(control_root: str, storage_options: StorageOptions, table_uuid: str, marks: CommitMarks) -> None:
    """Replace the table's marks. Two passes racing may write a lower mark over a higher one, which costs a re-read of
    versions already announced and never skips one: every mark a pass writes is one it verified."""
    put_record(control_root, storage_options, record_key(MARKS_PREFIX, "table", table_uuid), marks.model_dump())
