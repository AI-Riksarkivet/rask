"""A branch's reference file, read and written where a recoverable delete hides the branch ([[LH-429]]).

``lance_docs/lance/format/table/branch_tag.md`` § Branch Metadata Path: ``_refs/branches/<name>.json`` under the table's
root, the name URL-encoded (:func:`lakehouse.lancekit.branch_layout.ref_file_name`).

Every function is BLOCKING IO; callers threadpool it.
"""

from pyarrow import fs as pafs

from catalog.core.table_objects import dataset_fs
from lakehouse.lancekit import branch_layout
from lakehouse.records.branch_trash import HiddenBranch
from lakehouse.storage.options import StorageOptions


def reference(name: str) -> str:
    """The reference file of the branch ``name``, relative to its table's root."""
    return f"_refs/branches/{branch_layout.ref_file_name(name)}"


def read_reference(location: str, so: StorageOptions, name: str) -> str:
    """The text of the reference file of the branch ``name`` of the table at ``location``.

    Raises:
        OSError: The file is missing, or the store refused the read.
    """
    filesystem, base = dataset_fs(location, so)
    with filesystem.open_input_stream(f"{base.rstrip('/')}/{reference(name)}") as stream:
        return stream.read().decode()


def reference_stands(location: str, so: StorageOptions, name: str) -> bool:
    """Whether the table at ``location`` holds a reference file for the branch ``name``."""
    filesystem, base = dataset_fs(location, so)
    return filesystem.get_file_info(f"{base.rstrip('/')}/{reference(name)}").type != pafs.FileType.NotFound


def write_reference(hidden: HiddenBranch, so: StorageOptions) -> None:
    """Put back the reference file ``hidden`` kept, unchanged."""
    filesystem, base = dataset_fs(hidden.location, so)
    with filesystem.open_output_stream(f"{base.rstrip('/')}/{reference(hidden.name)}") as stream:
        stream.write(hidden.reference.encode())


def is_hidden(hidden: HiddenBranch, so: StorageOptions) -> bool:
    """Whether the branch ``hidden`` records is hidden now: its reference file is gone, or is back only for the delete
    for good. A record beside a reference file otherwise is a hide that never finished, and its branch stands."""
    return hidden.purging or not reference_stands(hidden.location, so, hidden.name)
