"""Serialize a generated lance-namespace model to a JSON-able value.

JSON endpoints use typed ``response_model`` for serialization; this helper is
the fallback for the few non-typed payloads (e.g. the structured explain /
analyze plan results returned as plain text).
"""

from __future__ import annotations

from typing import Any


def dump(obj: Any) -> Any:
    """Return a JSON-able value via ``to_dict()`` / ``model_dump`` (alias-keyed); ``None`` → ``{}``."""
    if obj is None:
        return {}
    if isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return model_dump(by_alias=True, exclude_none=True)
    return obj
