"""Compute's closed audit vocabulary: every action it records on the ``lance.audit`` trail is one of these.

A client of the lakehouse owns its vocabulary, so the lakehouse's ``AuditAction`` names none of it.
"""

from __future__ import annotations

from enum import StrEnum


class ComputeAuditAction(StrEnum):
    """Every action compute records."""

    COMPUTE_READ = "compute_read"
