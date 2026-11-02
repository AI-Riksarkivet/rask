"""A Lance version's commit time, read in the right timezone."""

from collections.abc import Mapping
from datetime import UTC, datetime


def committed_at(version: Mapping[str, object]) -> datetime:
    """The commit time of one ``LanceDataset.versions()`` entry, as an aware UTC datetime.

    pylance builds ``timestamp`` with ``datetime.fromtimestamp`` and no zone, so it is naive LOCAL
    time: stamping UTC onto it is wrong by the host's offset, and comparing it with an aware time raises.

    Args:
        version: One entry of ``LanceDataset.versions()``.

    Returns:
        The same instant, in UTC.

    Raises:
        TypeError: If the entry's ``timestamp`` is not a datetime.
    """
    stamp = version.get("timestamp")
    if not isinstance(stamp, datetime):
        raise TypeError(f"Lance version {version.get('version')!r}: timestamp must be a datetime, got {type(stamp).__name__}: {stamp!r}")
    return stamp.astimezone(UTC)
