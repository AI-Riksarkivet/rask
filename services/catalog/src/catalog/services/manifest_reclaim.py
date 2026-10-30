"""Reclaim the history of each root's ``__manifest``, the ``dir`` backend's namespace index ([[LH-251]]).

A ``dir`` root keeps its namespaces and tables as rows of one Lance table, ``<root>/__manifest``
(``lance_docs/ns_catalog/catalog/dir/index.md`` § V2: Manifest), and every namespace operation rewrites it whole as a new
version. Nothing in the backend reclaims those versions: its rewrite commits with ``skip_auto_cleanup`` (lance v13.0.0
``rust/lance-namespace-impls/src/dir/manifest.rs``), so ``lance.auto_cleanup.*`` on the index does nothing. Measured on
pylance 13.0.0: one version and one data file per operation, each a full copy, and a dropped id readable at latest - 1.

As a Lance table the index takes the format's own reclamation, so the catalog, its only writer, runs
``cleanup_old_versions`` on it under its own store identity, removing versions older than ``manifest_retain_seconds``.
That is safe for the catalog's connections only because none is served past ``connection_max_age_seconds``
(``catalog.core.namespace.NamespaceConnections``). The index carries no tag or branch, so no history fence applies.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

import lance
import pyarrow.fs as pafs
from pydantic import BaseModel

from catalog.core.config import Settings, shared_lance_session
from catalog.services import warehouses
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


log = logging.getLogger(__name__)

MANIFEST_TABLE: Final = "__manifest"


class RootReclaim(BaseModel):
    """What one root's reclaim removed, and how many versions of the index it kept."""

    root: str
    versions_removed: int
    bytes_removed: int
    versions_kept: int


def roots(settings: Settings) -> list[str]:
    """Every root an index can be at, once each: the catalog's own, and every registered warehouse's.

    A deactivated warehouse's root is reclaimed too: its index is the catalog's bookkeeping, not the tenant's data, and
    the ids an offboarding drops are what the history would otherwise keep readable.
    """
    records = warehouses.list_warehouses(settings.registry_root, settings.storage_options())
    found = [settings.root, *(str(record["root_uri"]) for record in records if record.get("root_uri"))]
    return list(dict.fromkeys(root.rstrip("/") for root in found))


def reclaim(root: str, storage_options: StorageOptions, *, older_than: timedelta) -> RootReclaim | None:
    """Remove the versions of ``root``'s index older than ``older_than``; ``None`` when the root holds no index.

    Raises:
        OSError: The store did not answer.
        ValueError: Lance refused the open or the cleanup.
        RuntimeError: Lance failed otherwise.
    """
    fs, base = fs_and_base(root, storage_options)
    if fs.get_file_info(f"{base}/{MANIFEST_TABLE}/_versions").type != pafs.FileType.Directory:
        return None
    index = lance.dataset(f"{root}/{MANIFEST_TABLE}", storage_options=storage_options, session=shared_lance_session())
    stats = index.cleanup_old_versions(older_than=older_than, error_if_tagged_old_versions=False)
    return RootReclaim(root=root, versions_removed=stats.old_versions, bytes_removed=stats.bytes_removed, versions_kept=len(index.versions()))
