"""The table objects a writer-tier credential may write, and the one it may never write ([[LH-202]], [[LH-229]]).

`lance_docs/file_format.md` fixes a dataset's layout: data files under ``data/``, manifests under
``_versions/``, transaction files under ``_transactions/``, deletion files under ``_deletions/``, indices
under ``_indices/``, branch and tag pointers under ``_refs/``, each branch's own files under
``tree/<branch>/`` and the MemWAL under ``_mem_wal/``.

A writer-tier client is a managed-versioning writer (ADR 0157): it writes everything a commit needs except
the commit itself. Measured with stock pylance 13.0.0 against MinIO ``RELEASE.2025-04-22T22-12-26Z`` through
a namespace advertising ``managed_versioning=true`` (LH-469, #594): a create and an append put
``data/<file>.lance``, ``_transactions/<n>-<uuid>.txn`` and the staged ``_versions/<n>.manifest-<uuid>``, and
the namespace finalized ``_versions/<n>.manifest``; the writer fails without the transaction grant. Delete and
update stage ``_deletions/``, and an index build stages ``_indices/<uuid>/``
(``lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md`` § CreateTableVersion).

The final manifest is denied explicitly rather than left out of the allow-list: ``_versions/*.manifest-*``
also matches ``<n>.manifest-<uuid>.manifest``, which pylance reads as a final manifest (measured: one planted
there left the table unopenable, "multiple manifest naming schemes"). ``_refs/`` and the version hint are
granted nothing.

The catalog's vending door reads this module, so what a writer-tier credential may write is named in one place.
"""

from __future__ import annotations

from typing import Final


#: Where a client writes data files (and blob sidecars beneath them).
DATA_DIR: Final = "data"

#: The object globs a writer-tier credential may put, relative to the ref it writes (the table, or ``tree/<branch>``).
WRITER_PUT_GLOBS: Final = (f"{DATA_DIR}/*", "_versions/*.manifest-*", "_transactions/*", "_deletions/*", "_indices/*")

#: The final manifest name, in both naming schemes, which only the catalog writes: putting one commits a version.
FINAL_MANIFEST_GLOB: Final = "_versions/*.manifest"
