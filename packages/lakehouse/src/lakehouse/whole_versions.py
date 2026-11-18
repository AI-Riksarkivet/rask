"""Whether a version of a table still holds every file it names: what a door judges before it pins or brings one back (ADR 0243).

LANCE'S CLEANUP DELETES A VERSION'S FILES BEFORE ITS MANIFEST. Measured on pylance 13.0.0 (ADR 0243 §3): one
``cleanup_old_versions`` deletes the transaction files, then the data files of the versions it removes, and their
manifests last, and a delete the store refuses is counted (``CleanupStats.failed_deletes``) rather than raised. So a
cleanup that stops between the two (its process killed, or its credential expired so the store refuses the rest) leaves
those versions standing over deleted files until a later cleanup removes them. A tag or a branch on one, a restore of
one, or a clone reading one would answer for a version no reader can read.

JUDGED UNDER THE TABLE'S HISTORY FENCE, THE ANSWER STANDS. Every caller asks while it holds the table's history as ref
work (:class:`lakehouse.history_fences.TableHistory`), which every reclaim excludes, so no file a version names is deleted
between the judgement and the ref; and once the ref stands, the version is pinned or is the head, which no cleanup removes.

What a version names under its own ref's directory is judged: its fragments' data files and their blob sidecars, their
deletion files, and its indices' directories. A file it names through another base (a branch's parent, a clone's source,
one of the table's data directories) is that base's, which no cleanup of this ref deletes while a retained version names
it. Its transaction file is not: no read of the version needs it, and the cleanup deletes it first.

Every function is blocking IO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lakehouse import stored_objects
from lakehouse.base_refs import decoded_path, key_uri, store_of


if TYPE_CHECKING:
    import lance

    from lakehouse.storage_options import StorageOptions


#: The directories under a ref a version's own files lie in.
_DIRECTORIES = ("data", "_deletions", "_indices")


def missing_files(version: lance.LanceDataset, ref_root: str, storage_options: StorageOptions) -> list[str]:
    """Each file ``version`` names under ``ref_root`` (the directory of the ref it is a version of) that the store no longer
    holds, by its path under that directory; ``[]`` for a whole version.

    One listing of each of the ref's file directories the version names anything in, a request per thousand objects.

    Raises:
        OSError: The store refused, or could not be reached for, a listing.
    """
    files: set[str] = set()
    directories: set[str] = set()
    for fragment in version.get_fragments():
        files.update(f"data/{data_file.path}" for data_file in fragment.data_files() if data_file.base_id is None)
        deletion = fragment.metadata.deletion_file
        if deletion is not None and deletion.base_id is None:
            files.add(deletion.path(fragment.fragment_id))
    directories.update(f"_indices/{segment.uuid}/" for index in version.describe_indices() for segment in index.segments if segment.base_id is None)
    if not files and not directories:
        return []
    key = decoded_path(ref_root).rstrip("/")
    held: set[str] = set()
    for directory in _DIRECTORIES:
        if any(path.startswith(f"{directory}/") for path in files | directories):
            listed = stored_objects.iter_under(key_uri(store_of(ref_root), f"{key}/{directory}"), storage_options)
            held.update(stored.path.lstrip("/").removeprefix(f"{key}/") for stored in listed)
    gone = [path for path in files if path not in held]
    gone += [directory for directory in directories if not any(path.startswith(directory) for path in held)]
    return sorted(gone)
