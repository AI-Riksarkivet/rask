"""Word-alignment JSON decoding — vendored into the backend (P2.8 standalone).

Vendored from the pipeline package that has since been dissolved, so this is now the ONE
implementation rather than a copy to keep in sync: the serving layer needed exactly this
decoder and none of the rest. The stored shape is frozen by the tables in practice.
"""

from __future__ import annotations

import json
from typing import Any


def parse_alignments_json(raw: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Decode the alignments JSONB column to a Python list.

    Returns an empty list on null, missing, malformed, or non-list input — so
    the annotated return type holds for every branch (Lance may hand back either
    the raw JSON string or an already-decoded value).
    """
    if not raw:
        return []
    decoded = raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return decoded if isinstance(decoded, list) else []
