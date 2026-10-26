"""Whether compacting a dataset is refused: the one gate maintenance's due list and the catalog's ``version/create``
judge both ask, so a table listed as due is one whose compaction the judge admits (ADR 0162).

A branch is a shallow clone that lives inside its table (``lance_docs/lance/format/table/branch_tag.md`` § Branch Dataset Layout,
``lance_docs/lance/format/table/layout.md`` § Shallow Clone): it reads its parent's fragments through a
dataset-root base on the table's root, and a branch of a branch through its parent's ``tree/<name>`` too, so every
branch sets flag 16. Those bases are the table's own history, and a branch's compaction copies what it inherited into
its own ``tree/<name>/data/`` on purpose (measured on pylance 13.0.0: a stock compaction on a branch rewrote the
inherited fragments there and main stayed at its version). So a base inside the table, by the rule the base judge
calls a table's own (:func:`service_kit.lakehouse.base_registry.is_own`), is not weighed, and every other base is
weighed by :func:`service_kit.lakehouse.features.describe_compaction_unsupported_flags`.
"""

from __future__ import annotations

import logging

from service_kit.lakehouse.base_registry import is_own
from service_kit.lakehouse.features import (
    FLAG_BASE_PATHS,
    BasePathRef,
    CompactionBases,
    FragmentCarrier,
    data_file_base_paths,
    describe_compaction_unsupported_flags,
    gather_compaction_bases,
    manifest_feature_flags,
)
from service_kit.lakehouse.objectfs import StorageOptions, dataset_root_probe


log = logging.getLogger(__name__)


def compaction_refusal(ds: FragmentCarrier, table_location: str, storage_options: StorageOptions) -> str | None:
    """Why compacting ``ds`` is refused, or ``None``.

    ``table_location`` is the root of the table ``ds`` belongs to, for a branch's handle too. The base readings are
    gathered only when flag 16 is set, the one place the gate costs IO (one probe per base outside the table), and
    ``dataset_root_probe`` binds the probe to the table's store: a scheme-less base probed directly reads as a local
    path, finds nothing and answers "not a dataset root", a wrong permit on a real clone. A dataset with no location
    cannot be probed, so its bases stay unknown and flag 16 refuses.
    """
    reader, writer = manifest_feature_flags(ds)
    if not (table_location and (reader | writer) & FLAG_BASE_PATHS):
        return describe_compaction_unsupported_flags(reader, writer, None)
    probe = dataset_root_probe(table_location, storage_options)
    gathered = gather_compaction_bases(ds, lambda path: _own(table_location, path) or probe(path))
    if not any(_own(table_location, base.path) for base in gathered.bases):
        return describe_compaction_unsupported_flags(reader, writer, gathered)
    foreign = _outside_the_table(ds, gathered, table_location)
    if not foreign.bases and foreign.data_resolves_through_a_base is False:
        return describe_compaction_unsupported_flags(reader & ~FLAG_BASE_PATHS, writer & ~FLAG_BASE_PATHS, None)
    return describe_compaction_unsupported_flags(reader, writer, foreign)


def _own(table_location: str, path: str) -> bool:
    """Whether a base at ``path`` lies inside the table rooted at ``table_location``, in its store."""
    return is_own(table_location, BasePathRef(path=path))


def _outside_the_table(ds: FragmentCarrier, gathered: CompactionBases, table_location: str) -> CompactionBases:
    """The evidence about the bases outside the table: those bases, and whether a data file resolves through one.

    The second is ``None`` when the fragments' bases cannot be read, which the gate weighs as a refusal.
    """
    try:
        through: bool | None = any(not _own(table_location, path) for path in data_file_base_paths(ds))
    except Exception as exc:  # noqa: BLE001 — an unread base is a refusal, never a permit
        log.warning("compaction_data_file_bases_unread", extra={"location": table_location, "error": str(exc)})
        through = None
    return CompactionBases(bases=[base for base in gathered.bases if not _own(table_location, base.path)], data_resolves_through_a_base=through)
