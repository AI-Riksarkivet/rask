"""Flows's closed audit vocabulary: every action it records on the ``lance.audit`` trail is one of these.

A client of the lakehouse owns its vocabulary, so the lakehouse's ``AuditAction`` names none of it.
"""

from __future__ import annotations

from enum import StrEnum


class FlowsAuditAction(StrEnum):
    """Every action flows records."""

    FLOWS_READ = "flows_read"
    FLOWS_RUN = "flows_run"
    FLOWS_RUN_READ = "flows_run_read"
    FLOWS_RUN_TERMINATE = "flows_run_terminate"
