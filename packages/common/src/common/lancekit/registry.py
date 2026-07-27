"""Lazy multi-dataset registry — one serving app, many Lance DBs.

Datasets are directories named ``<id>.lance`` under ``MEDIA_DB_ROOT``. Each is
opened on first use and cached with its validated
:class:`~common.lancekit.descriptor.DatasetDescriptor`; an invalid or missing
descriptor surfaces as a domain error, never a half-served dataset. The
legacy single-DB routes resolve through :meth:`default`, so the pre-split API
surface keeps working while new routes address datasets explicitly.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import lance
import lancedb
from pydantic import BaseModel, ConfigDict, PrivateAttr

from common.core.exceptions import NotFoundError
from common.lancekit import store
from common.lancekit.descriptor import DatasetDescriptor, load_dataset_descriptor
from common.lancekit.introspect import table_info


logger = logging.getLogger(__name__)


class DatasetHandle(BaseModel):
    """One opened dataset: its lancedb connection + merged descriptor.

    ``uri`` is the dataset root as a string (a local path or an ``s3://`` URI);
    ``storage_options`` is passed to every ``lance.dataset`` / ``take_blobs`` open
    (``None`` = local filesystem). ``path`` stays a ``Path`` for the local-only
    capability routes that haven't been threaded yet.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    path: Path
    uri: str
    db: lancedb.DBConnection
    descriptor: DatasetDescriptor
    storage_options: dict[str, str] | None = None

    _sync_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def table_uri(self, table: str) -> str:
        """The ``<name>.lance`` URI of one table (URI-safe join, no Path collapse)."""
        return store.join(self.uri, f"{table}.lance")

    def sync_table_info(self, table: str, observed_version: int) -> None:
        """Fold runtime drift back into the cached descriptor — best-effort.

        Called by the per-request open seams when they observe a table version the
        cached :class:`TableInfo` doesn't know (a write bumped it) or a table the
        descriptor has never seen (created after first ``get()``). Re-introspects
        ONLY that table — a full descriptor reload would rescan every table after
        every annotations save.

        The tables dict is rebound copy-on-write (never mutated in place) so
        concurrent readers iterating it can't hit ``RuntimeError``. An identity
        re-check under the lock makes any concurrent refresh satisfy the waiting
        observers instead of stampeding serial re-introspections. Introspection
        failures are swallowed (warning): the caller already holds a good open —
        a stale cache must never fail its request. Known limit: a drop+re-create
        landing on the same version number is indistinguishable from no drift.
        """
        info = self.descriptor.tables.get(table)
        if info is not None and info.version == observed_version:
            return
        with self._sync_lock:
            if self.descriptor.tables.get(table) is not info:
                return  # someone refreshed while we waited — their read is current
            try:
                fresh = table_info(self.table_uri(table), storage_options=self.storage_options)
            except Exception:
                logger.warning("table_info sync failed for %s/%s", self.id, table, exc_info=True)
                return
            self.descriptor.tables = {**self.descriptor.tables, table: fresh}


class UnknownDatasetError(LookupError):
    """No ``<id>.lance`` directory under the registry root."""


class DatasetRegistry:
    """Thread-safe lazy cache of :class:`DatasetHandle` by dataset id."""

    def __init__(
        self,
        root: str | Path,
        descriptor_dir: str | Path,
        default_id: str,
        storage_options: dict[str, str] | None = None,
    ) -> None:
        self._root = str(root)
        self._descriptor_dir = Path(descriptor_dir)
        self._default_id = default_id
        self._storage_options = storage_options
        self._handles: dict[str, DatasetHandle] = {}
        self._lock = threading.Lock()

    def list_ids(self) -> list[str]:
        return store.list_lance_stems(self._root, self._storage_options)

    def get(self, dataset_id: str) -> DatasetHandle:
        # A dataset id is a single flat directory stem: reject anything with a
        # path separator or dot segment so `..`/`/`/`.` can't escape the root,
        # alias the default, or mint duplicate caches (audit: path traversal via
        # the `dataset` query param). Ids must match what list_ids() advertises.
        if dataset_id in {"", ".", ".."} or "/" in dataset_id or "\\" in dataset_id:
            raise UnknownDatasetError(f"invalid dataset id {dataset_id!r}")
        with self._lock:
            handle = self._handles.get(dataset_id)
            if handle is not None:
                return handle
            uri = store.join(self._root, f"{dataset_id}.lance")
            if not store.exists(uri, self._storage_options):
                raise UnknownDatasetError(f"dataset {dataset_id!r} not found under {self._root}")
            descriptor = load_dataset_descriptor(uri, dataset_id, self._descriptor_dir, storage_options=self._storage_options)
            handle = DatasetHandle(
                id=dataset_id,
                path=Path(uri),
                uri=uri,
                db=lancedb.connect(uri, storage_options=self._storage_options),
                descriptor=descriptor,
                storage_options=self._storage_options,
            )
            self._handles[dataset_id] = handle
            logger.info("opened dataset %s (%d tables)", dataset_id, len(descriptor.tables))
            return handle

    def default(self) -> DatasetHandle:
        return self.get(self._default_id)


def table_dataset(handle: DatasetHandle, table: str) -> lance.LanceDataset:
    """Open one table of the dataset as a ``lance.LanceDataset`` (blob-capable).

    Opens over the object store when the dataset is S3-backed
    (``handle.storage_options``); the local existence check is skipped for URIs
    (``lance.dataset`` raises if the table is absent).
    """
    uri = handle.table_uri(table)
    # Local: a fast directory check gives a clean 404. Over an object store we
    # can't cheaply stat, so translate lance.dataset's own "not found" raise into
    # the same NotFoundError — otherwise a missing / mid-rebuild table would
    # escape as a raw 500 instead of the 404 the local path returns.
    if handle.storage_options is None and not (handle.path / f"{table}.lance").is_dir():
        raise NotFoundError(f"table {table!r} missing from dataset {handle.id!r}")
    try:
        ds = lance.dataset(uri, storage_options=handle.storage_options)
    except (ValueError, OSError) as e:
        msg = str(e).lower()
        if "not found" in msg or "does not exist" in msg:
            raise NotFoundError(f"table {table!r} missing from dataset {handle.id!r}") from e
        raise
    # Fold observed drift back into the cached descriptor (a write bumped the
    # version, or the table was created after the dataset was first opened) —
    # otherwise the registry's TableInfo stays frozen at first get() forever.
    handle.sync_table_info(table, int(ds.version))
    return ds
