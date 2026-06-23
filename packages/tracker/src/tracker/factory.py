"""Select a tracker backend from a SQLite file path or a PostgreSQL DSN."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.engine import make_url

from tracker.postgres import PostgresTracker
from tracker.protocol import TrackerProtocol
from tracker.sqlite import SqliteTracker


# A URL-ish scheme: 2+ chars so Windows drive letters (C:/...) stay paths.
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]+):/")


def redact_dsn(dsn: str) -> str:
    """Render a DSN with the password masked — safe for console output and logs."""
    return make_url(dsn).render_as_string(hide_password=True)


def create_tracker(target: str | Path, *, flush_every: int = 200) -> TrackerProtocol:
    """Create a tracker from ``target``.

    Targets with a PostgreSQL scheme (``postgres://``, ``postgresql://``,
    ``postgresql+<driver>://``) select :class:`PostgresTracker`; any other
    URL scheme is rejected loudly; everything else is treated as a SQLite
    database file path.
    """
    target_str = str(target)
    match = _SCHEME_RE.match(target_str)
    if match is None:
        return SqliteTracker(Path(target), flush_every=flush_every)

    scheme = match.group(1).lower()
    if scheme in ("postgres", "postgresql") or scheme.startswith("postgresql+"):
        # Rebuild '<scheme>://' — Path() normalizes '//' to '/', and this
        # also repairs a hand-typed single-slash DSN.
        rest = target_str.split(":", 1)[1].lstrip("/")
        return PostgresTracker(f"{scheme}://{rest}", flush_every=flush_every)

    raise ValueError(f"Unsupported tracker DSN scheme {scheme!r} — expected a SQLite file path or a postgresql:// DSN")
