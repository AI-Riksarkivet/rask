"""rahcp-tracker — pluggable transfer state tracking.

::

    from rahcp_tracker import TrackerProtocol, TransferStatus, SqliteTracker
"""

from rahcp_tracker.models import Transfer, TransferStatus
from rahcp_tracker.protocol import TrackerProtocol
from rahcp_tracker.sqlite import SqliteTracker

# Backwards-compatible alias
TransferTracker = SqliteTracker

__all__ = [
    "TrackerProtocol",
    "Transfer",
    "TransferStatus",
    "SqliteTracker",
    "TransferTracker",
]
