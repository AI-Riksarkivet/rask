"""Search's closed audit vocabulary: every action it records on the ``lance.audit`` trail is one of these.

A client of the lakehouse owns its vocabulary, so the lakehouse's ``AuditAction`` names none of it.
"""

from __future__ import annotations

from enum import StrEnum


class SearchAuditAction(StrEnum):
    """Every action search records."""

    SEARCH_CORPUS_READ = "search.corpus.read"
