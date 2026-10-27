"""The viewer's closed audit vocabulary: every action it records on the ``lance.audit`` trail is one of these.

A client of the lakehouse owns its vocabulary, so the lakehouse's ``AuditAction`` names none of it.
"""

from __future__ import annotations

from enum import StrEnum


class ViewerAuditAction(StrEnum):
    """Every action the viewer records."""

    VIEWER_CLIP_READ = "viewer.clip.read"
    VIEWER_CORPUS_READ = "viewer.corpus.read"
    VIEWER_DESCRIPTOR_READ = "viewer.descriptor.read"
    VIEWER_MEDIA_READ = "viewer.media.read"
    VIEWER_OBJECT_DOWNLOAD = "viewer.object.download"
    VIEWER_OBJECT_HEAD = "viewer.object.head"
    VIEWER_OBJECTS_LIST = "viewer.objects.list"
    VIEWER_PAGE_READ = "viewer.page.read"
    VIEWER_PAGES_LIST = "viewer.pages.list"
    VIEWER_STORES_LIST = "viewer.stores.list"
