"""What a door filed before its change, as a test reaches it: made older than the lease, so the finish judges it."""

from __future__ import annotations

import os

import pyarrow.fs as pafs

from catalog.core.config import get_settings
from catalog.services import announcement_intents
from lakehouse.objectfs import fs_and_base


def age_filings() -> None:
    """Stamp every standing filing on the catalog's local control root twice the lease earlier, as a door that stopped
    that long ago would have left it; the store's own stamp is what the finish reads a filing's age off."""
    settings = get_settings()
    fs, base = fs_and_base(settings.registry_root, settings.storage_options())
    aged = (announcement_intents.LEASE * 2).total_seconds()
    for info in fs.get_file_info(pafs.FileSelector(f"{base}/{announcement_intents.PREFIX}", recursive=True, allow_not_found=True)):
        if info.type == pafs.FileType.File and info.mtime is not None:
            stamp = info.mtime.timestamp() - aged
            os.utime(info.path, (stamp, stamp))
