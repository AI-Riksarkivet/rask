"""Whether a version of a table still holds the files its manifest names: the second line a door judges, under the marks of
:mod:`lakehouse.reclaim_marks`, before it pins or brings one back (ADR 0243).

THE FIRST LINE IS THE MARK. A cleanup marks the versions it will remove before its first delete, and a door refuses a
version a standing mark names whatever files still stand (``catalog.services.dataplane.require_whole``). This module
answers for a version no mark names: one a store lost a file of, or one a cleanup that wrote no mark half removed (an
erasure's, which ADR 0243 leaves to this line).

WHAT IS JUDGED, EXACTLY: of what the version names under its own ref's directory, each data file
(``fragment.data_files()``) and each deletion file is asked for by key, and each index segment's directory
(``_indices/<uuid>/``) is listed and its objects' bytes summed against the size the manifest records for the segment
(``IndexSegmentDescription.size_bytes``; measured on pylance 13.0.0 equal to the sum for BTREE, BITMAP, INVERTED and
IVF_PQ segments), or, where it records none, held to one object at least. A data file's blob sidecars
(``data/<stem>/<id>.blob``) and a managed blob object (``_blobs/<uuid>.blob``) are NOT judged: which a version holds is
written in its rows' descriptors, not its manifest (``lance_docs/lance/format/table/blob.md`` § Sidecar Resolution), and
reading every descriptor is a scan of the version. The mark is what refuses a version whose sidecars a cleanup took. A
file the version names through another base (a branch's parent, a clone's source, one of the table's data directories) is
that base's, which no cleanup of this ref deletes while a retained version names it; its transaction file is not judged:
no read of the version needs it.

WHAT IT COSTS: a HEAD per data and deletion file, :data:`lakehouse.stored_objects.MOST_AT_ONCE` at once, and a listing
per index segment, never a listing of the ref's whole ``data/`` (measured on RustFS 1.0.3, ADR 0243 §5).

Judged under the table's history fence held as ref work, which every reclaim excludes, the answer stands until the ref
does. Every function is blocking IO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lakehouse import stored_objects
from lakehouse.base_refs import decoded_path, key_uri, store_of


if TYPE_CHECKING:
    import lance

    from lakehouse.storage_options import StorageOptions


def missing_files(version: lance.LanceDataset, ref_root: str, storage_options: StorageOptions) -> list[str]:
    """Each file ``version`` names under ``ref_root`` (the directory of the ref it is a version of) that the store no longer
    holds as named, by its path under that directory; ``[]`` for a version holding every one this module judges.

    Raises:
        OSError: The store refused, or could not be reached for, a read: whether the version is whole is unknown, never
            answered as "not whole".
    """
    files: list[str] = []
    for fragment in version.get_fragments():
        files.extend(f"data/{data_file.path}" for data_file in fragment.data_files() if data_file.base_id is None)
        deletion = fragment.metadata.deletion_file
        if deletion is not None and deletion.base_id is None:
            files.append(deletion.path(fragment.fragment_id))
    segments = [segment for index in version.describe_indices() for segment in index.segments if segment.base_id is None]
    root = key_uri(store_of(ref_root), decoded_path(ref_root).rstrip("/"))
    held = stored_objects.present(root, storage_options, sorted(set(files)))
    gone = [path for path in sorted(set(files)) if path not in held]
    from botocore.exceptions import BotoCoreError, ClientError

    for segment in segments:
        directory = f"_indices/{segment.uuid}"
        try:
            stored = stored_objects.list_under(f"{root}/{directory}", storage_options)
        except (ClientError, BotoCoreError) as exc:
            raise OSError(f"the store did not answer listing {directory} of {root}: {exc}") from exc
        recorded = int(segment.size_bytes or 0)
        if not stored or (recorded and sum(item.size for item in stored) != recorded):
            gone.append(f"{directory}/")
    return gone
