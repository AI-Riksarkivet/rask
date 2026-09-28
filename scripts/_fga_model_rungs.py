"""Flatten an OpenFGA authorization model to one `type#relation` per rung, sorted.

ONE implementation for both sides of `fga-store-check.sh`'s report (`_fga_store_check.py`): the repo's
`model.json` and the model read off the running store. Two flatteners would be a fourth and fifth copy
of the thing that check exists to catch, and the estate has already paid for that class of drift once.
"""

from __future__ import annotations

from typing import Any


def rungs(model: dict[str, Any]) -> list[str]:
    return sorted(f"{t['type']}#{relation}" for t in model.get("type_definitions", []) for relation in (t.get("relations") or {}))
