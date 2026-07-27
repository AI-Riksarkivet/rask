"""Domain errors for the ratch library.

Library code raises :class:`RatchError` (never ``SystemExit`` — exiting the
process is the CLI's decision, and a raised exit kills embedding callers and
tests). The CLI entry point (``ratch.__main__.main``) maps it to a clean
stderr message + exit code 1.
"""

from __future__ import annotations


class RatchError(Exception):
    """A user-actionable failure (bad input path, missing file, empty dir).

    The message is shown verbatim to the CLI user — keep it specific enough to
    act on (what was looked for, where).
    """
