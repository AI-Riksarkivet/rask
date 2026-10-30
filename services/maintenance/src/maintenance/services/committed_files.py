"""The files the catalog recorded at each commit, read again: what is no longer as committed is named ([[LH-419]]).

A writer's vend may put over any file a commit adds under its table (``table_layout.WRITER_PUT_GLOBS``), and a maintain
vend may also delete one. Lance checks no file's content: measured on pylance 13.0.0, a same-size overwrite of a data
file is served silently, while Lance requires a published payload to stay immutable while a protected snapshot references
it (``lance_docs/lance/format/table/blob.md:254-257``). The catalog records every file a commit adds, with its ETag and
size, before the version lands (``service_kit.lakehouse.file_identities``), and the sweep reads each one again.

A recorded file is named when the store holds it with another ETag or size, or no longer holds it while a retained
version of any ref still names it. No legitimate writer does either: every Lance file name is minted once, and cleanup
deletes only what no retained version references. The store's object versions are what restores one
(``.claude/skills/rask-rustfs`` § 8).

A record is forgotten only once no retained version names any of its files and the store holds none of them: cleanup
reclaimed them, or the commit lost its race and Lance reclaimed what it wrote. Absence alone forgets nothing, since a
file deleted and put again would otherwise read as reclaimed and then as new. A commit still in flight holds its files,
so its record stays.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

import lance
from pydantic import BaseModel, ConfigDict, Field

from maintenance.services import base_reclaim
from service_kit.lakehouse import file_identities, stored_objects
from service_kit.lakehouse.base_refs import decoded_path
from service_kit.lakehouse.branch_layout import BRANCH_CONTAINER
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


class FileCheck(BaseModel):
    """What one read of a table's recorded files found."""

    model_config = ConfigDict(frozen=True)

    #: Each recorded file not as committed, by its path under the table: held with another ETag or size, or gone while a
    #: retained version names it.
    overwritten: list[str] = Field(default_factory=list)
    #: The records forgotten because no retained version names their files and the store holds none of them.
    forgotten: int = 0


def check(
    table_root: str,
    table_uuid: str,
    *,
    open_table: Callable[[], lance.LanceDataset],
    data_options: StorageOptions,
    control_root: str,
    control_options: StorageOptions,
) -> FileCheck:
    """Read every file the catalog recorded for the table at ``table_root`` (the store's own spelling of it) again.

    ``open_table`` opens the table's latest version, and is called only when a recorded file is gone: the retained
    versions say whether it was reclaimed.

    Raises:
        ValueError: A record is not one a commit wrote, so the files it names cannot be checked.
        OSError: The store refused, or could not be reached for, a read or a delete.
    """
    committed = file_identities.recorded(control_root, control_options, table_uuid)
    held = stored_objects.identities(table_root, data_options, sorted({file.path for commit in committed for file in commit.files}))
    absent = {file.path for commit in committed for file in commit.files if file.path not in held}
    named = _named_by_a_retained_version(open_table(), table_root, absent) if absent else set()
    overwritten: list[str] = []
    for commit in committed:
        for file in commit.files:
            now = held.get(file.path)
            if now == file.identity or (now is None and file.path not in named):
                continue
            overwritten.append(file.path)
            log.error(
                "committed_file_overwritten",
                extra={
                    "table_uuid": table_uuid,
                    "path": file.path,
                    "transaction_uuid": commit.transaction_uuid,
                    "recorded": file.identity.model_dump(),
                    "found": None if now is None else now.model_dump(),
                },
            )
    reclaimed = [commit for commit in committed if all(file.path in absent and file.path not in named for file in commit.files)]
    for commit in reclaimed:
        file_identities.forget(control_root, control_options, table_uuid, commit.transaction_uuid)
    return FileCheck(overwritten=sorted(overwritten), forgotten=len(reclaimed))


def _named_by_a_retained_version(table: lance.LanceDataset, table_root: str, paths: Iterable[str]) -> set[str]:
    """Those of ``paths`` (relative to ``table_root``) that a retained version of main or of a branch names.

    A version names its fragments' data files, their blob sidecars and deletion files, its indices' files and its own
    transaction file. A branch names its own under ``tree/<branch>/`` and its parent's through a dataset-root base on a
    directory of the table (``lance_docs/lance/format/table/layout.md`` § Base Path System).
    """
    named: set[str] = set()
    for branch, version in base_reclaim.every_retained_version(table):
        named |= _named_by(version, "" if branch is None else f"{BRANCH_CONTAINER}/{branch}/", decoded_path(table_root))
    return {path for path in paths if _is_named(path, named)}


def _named_by(version: lance.LanceDataset, ref: str, root: str) -> set[str]:
    """What one version names, relative to the table's root ``root``: files by path, and index directories ending ``/``."""
    within = {base_id: where for base_id, base in version.base_paths().items() if base.is_dataset_root and (where := _within(root, base.path)) is not None}

    def at(base_id: int | None) -> str | None:
        return ref if base_id is None else within.get(base_id)

    named: set[str] = set()
    for fragment in version.get_fragments():
        for data_file in fragment.data_files():
            if (where := at(data_file.base_id)) is not None:
                named.add(f"{where}data/{data_file.path}")
        deletion = fragment.metadata.deletion_file
        if deletion is not None and (where := at(deletion.base_id)) is not None:
            named.add(f"{where}{deletion.path(fragment.fragment_id)}")
    for index in version.list_indices():
        if (where := at(index.get("base_id"))) is not None:
            named.add(f"{where}_indices/{index['uuid']}/")
    transaction = version.read_transaction(version.version)
    if transaction is not None:
        named.add(f"{ref}_transactions/{transaction.read_version}-{transaction.uuid}.txn")
    return named


def _within(root: str, base_path: str) -> str | None:
    """The table-relative directory a dataset-root base on ``root`` names, ending ``/``; ``None`` for one outside it."""
    path = decoded_path(base_path)
    if path == root:
        return ""
    return f"{path.removeprefix(f'{root}/')}/" if path.startswith(f"{root}/") else None


def _is_named(path: str, named: set[str]) -> bool:
    """A file named itself, a blob sidecar of a named data file, or a file of a named index directory."""
    head = path.rpartition("/")[0]
    return path in named or f"{head}.lance" in named or any(path.startswith(directory) for directory in named if directory.endswith("/"))
