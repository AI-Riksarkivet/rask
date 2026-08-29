"""A `#:` comment is an ATTRIBUTE doc — it must have an attribute under it (PS-22).

`submit.py` carried two `#:` lines describing "consecutive poll failures tolerated before giving up",
a blank line, and then `class RayJobError`. The constant they documented was deleted with the
`while True: sleep()` completion poll (A13); the prose outlived it and read, to anyone opening the
module, as though a poll-tolerance knob still existed. Sphinx would attach it to nothing.

The gate is over the whole package, not that one file: this is the sort of rot that comes back, and
`#:` is used correctly in five other modules, so the rule has real content.
"""

from __future__ import annotations

import re
from pathlib import Path


_SRC = Path(__file__).resolve().parents[1] / "src" / "ray_kit"
#: What a `#:` block is allowed to be followed by: an assignment (`X = …`) or an annotated one
#: (`x: T = …` / `x: T`), at any indentation — i.e. an actual attribute.
_ATTRIBUTE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*(?::[^=]+)?(?:=|$)")


def _orphans(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#:"):
            continue
        if i + 1 < len(lines) and lines[i + 1].lstrip().startswith("#:"):
            continue  # mid-block
        following = lines[i + 1] if i + 1 < len(lines) else ""
        if not _ATTRIBUTE.match(following):
            found.append((i + 1, line.strip()))
    return found


def test_every_attribute_doc_comment_documents_an_attribute() -> None:
    orphans = {path.name: _orphans(path) for path in sorted(_SRC.rglob("*.py"))}
    offending = {name: rows for name, rows in orphans.items() if rows}
    assert not offending, f"`#:` prose with no attribute under it (the constant it described is gone): {offending}"
