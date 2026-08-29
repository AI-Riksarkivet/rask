"""Shared classification of backend error messages.

pylance (probed at 9.0.0) exposes no typed error for a missing table/version —
the message is the only signal there is — so the registry (``table_dataset``)
and the reader (``_at_version``) both classify by substring. As two inline
copies they had already drifted: the reader missed "does not exist", the wording
object stores actually produce for a missing path, so the same condition was a
404 on one path and a raw 500 on the other. One vocabulary, one place.

The writer's ``_COMMIT_CONFLICT_MARKERS`` deliberately stay in ``writer.py``:
a lost OCC race is a different condition with a different remedy (409, re-read
and re-send), not a variant of not-found.
"""

from __future__ import annotations


_NOT_FOUND_MARKERS = ("not found", "does not exist")


def is_not_found(exc: BaseException) -> bool:
    """Whether the backend's error message says the table/version is MISSING."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _NOT_FOUND_MARKERS)
