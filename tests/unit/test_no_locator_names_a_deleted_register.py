"""A pointer at a root register names a file a reader can open.

Root `open_*.md` registers are ephemeral by design — they are drained and deleted, and that is the
intended end of one. What is not intended is the citations outliving them. `test_every_decisions_citation_resolves`
already checks the FORWARD direction (a citation of a settled-decisions heading lands somewhere real); this
is the backward one, and the drain of 2026-09-05 proved it was missing: three files still carried
`open_python-audit P0` / `X3` and one carried `open_python-audit.findings.json:3061` after the ledger
was deleted, because the repoint pass walked `.py`/`.yaml`/`.md` under seven source roots and those
three were `pyproject.toml`, `Makefile` and `deploy/`.

A LOCATOR IS THE THING CHECKED, not a mention. `open_python-audit.md` inside a sentence about its own
deletion is prose and stays; `open_python-audit P0` is an instruction to go and read row P0, and a
reader who follows it finds nothing. The difference is whether the reference points INTO the file.

The scan is the whole repo rather than a root list, for the reason the drain demonstrated: the files
that dangled were the ones no list included.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

#: `open_<name>` optionally with its extension, followed by something that points INTO it: a line
#: number (`:3061`), a row id (`X3`, `DUP-16`, `P0`), or a section mark (`§ Q3`).
_LOCATOR = re.compile(
    r"(open_[a-z0-9_.-]*?)(?:\.(?:md|json))?"  # the register, extension optional
    r"(?:"
    r":(\d+)"  # :3061
    r"|[ `]+(?:§ ?)?([A-Z][A-Z0-9]*-?\d+[a-z]?)\b"  # X3 / DUP-16 / P0 / Q3
    r")"
)

_TEXT = {".py", ".md", ".yaml", ".yml", ".toml", ".tpl", ".sh", ".json", ".ts", ".svelte", ""}


def _tracked() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / name for name in out.stdout.split("\0") if name and Path(name).suffix in _TEXT]


def _register_exists(stem: str) -> bool:
    return any((REPO / f"{stem}{ext}").exists() for ext in ("", ".md", ".json", ".findings.json"))


#: EMPTY, and kept as the shape rather than deleted. The fifteen locators it held were repointed
#: 2026-09-06 — at eighteen of the twenty-one sites the reasoning was already stated inline, so the
#: honest fix was dropping a dead id rather than inventing a destination for it. An entry belongs
#: here only while a repoint is genuinely deferred, and `test_the_carried_list_only_shrinks` refuses
#: one that is not still dangling.
_CARRIED: frozenset[str] = frozenset()


def test_no_locator_points_into_a_register_that_is_gone() -> None:
    dangling: dict[str, list[str]] = {}
    for path in _tracked():
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for stem, line, row in _LOCATOR.findall(text):
            if _register_exists(stem):
                continue
            locator = f"{stem} {line or row}"
            if locator in _CARRIED:
                continue
            dangling.setdefault(locator, []).append(str(path.relative_to(REPO)))
    assert not dangling, "pointers into a register that no longer exists: " + "; ".join(
        f"{k!r} <- {', '.join(sorted(set(v))[:3])}" for k, v in sorted(dangling.items())
    )


def test_the_carried_list_only_shrinks() -> None:
    """A baseline nobody re-derives is how a gate becomes decoration. Every entry must still be a
    real dangle; one that has been repointed is deleted from the list, not left as cover."""
    live: set[str] = set()
    for path in _tracked():
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for stem, line, row in _LOCATOR.findall(text):
            if not _register_exists(stem):
                live.add(f"{stem} {line or row}")
    stale = sorted(_CARRIED - live)
    assert not stale, f"repointed but still listed as carried debt — delete these entries: {stale}"
