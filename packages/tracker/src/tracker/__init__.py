"""tracker — pluggable transfer state tracking.

::

    from tracker import TrackerProtocol, TransferStatus, create_tracker
"""

from tracker.factory import create_tracker, redact_dsn
from tracker.models import Transfer, TransferStatus
from tracker.postgres import PostgresTracker
from tracker.protocol import TrackerProtocol
from tracker.sqlite import SqliteTracker


# Backwards-compatible alias
TransferTracker = SqliteTracker

__all__ = [
    "PostgresTracker",
    "SqliteTracker",
    "TrackerProtocol",
    "Transfer",
    "TransferStatus",
    "TransferTracker",
    "create_tracker",
    "redact_dsn",
]
