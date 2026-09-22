"""Flatten an OpenFGA authorization model to one `type#relation` per line, sorted.

ONE implementation for both sides of `fga-store-check.sh` — the repo's `model.json` and the JSON read
off the running store. Two flatteners would be a fourth and fifth copy of the thing that check exists
to catch, and the estate has already paid for that class of drift once.

Reads a path argument, or stdin when given none.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def rungs(model: dict[str, Any]) -> list[str]:
    return sorted(f"{t['type']}#{relation}" for t in model.get("type_definitions", []) for relation in (t.get("relations") or {}))


def main() -> int:
    raw = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()  # noqa: SIM115 — read once, no handle kept
    print("\n".join(rungs(json.loads(raw))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
