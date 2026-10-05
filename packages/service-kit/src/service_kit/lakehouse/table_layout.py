"""The table directories a writer-tier credential may write into ([[LH-202]]).

`lance_docs/file_format.md` fixes a dataset's layout: data files under ``data/``, manifests under
``_versions/``, transaction files under ``_transactions/``, deletion files under ``_deletions/``, indices
under ``_indices/``, branch and tag pointers under ``_refs/``, each branch's own files under
``tree/<branch>/`` and the MemWAL under ``_mem_wal/``. Of these, only ``data/`` holds files a client
writes before an append is committed. Measured on pylance 12.0.0 with
``LANCE_LOG=lance::events::file_audit`` and a listing before and after: ``write_fragments`` creates
``data/<file>.lance`` and, for a blob v2 column, ``data/<file>/<n>.blob``, and on a branch handle the
same under ``tree/<branch>/data/``; it writes nothing else.

The catalog's vending door and ingest's staging ledger both read this module, so the directory a writer
may write and the directory ingest writes cannot drift apart.
"""

from __future__ import annotations

from typing import Final


#: Where a client writes data files (and blob sidecars beneath them) before ``/commit`` folds them into a version.
DATA_DIR: Final = "data"

#: Ingest's staging ledger: one manifest per staged batch plus the run's unit list, written, read and
#: purged by the run that owns them. Not a Lance directory, so no Lance operation reads or rewrites it.
INGEST_STAGING_DIR: Final = "_ingest_staging"
