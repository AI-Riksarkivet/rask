"""`open_backlog_left.md` states counts, and this re-derives them from the rows.

A register's header is the only part most readers read, and this estate has been bitten by headers
that disagreed with their own rows: `open_estate-verification.md` carried a context sentence its own
row 15 falsified, and `open_python-audit.md` reported OPEN counts that included corrections to its own
false claims. A count nobody can check is a claim, not a measurement.

Three shapes only, and each one has failed here before:

* the phase table's per-phase counts must match the items under each phase heading;
* items are numbered continuously, so a duplicate or a gap cannot hide an item that was dropped or
  double-listed when a section was edited;
* the FOCUS block must exist and stay small. **This one is load-bearing beyond tidiness**: the Stop
  hook in `.claude/settings.local.json` slices exactly that block and injects it verbatim on every
  stop. Its predecessor grew to 23,362 characters by hoarding the record of the work it caused, which
  is the failure this cap exists to prevent.

Deliberately NOT a whole-file lint. Everything else about the prose is a reader's job.
"""

from __future__ import annotations

import re
from pathlib import Path


BACKLOG = Path(__file__).resolve().parents[2] / "open_backlog_left.md"

#: A rendered item: `**<n>. <title>**` at the start of a line. The number is the continuity key.
_ITEM = re.compile(r"^\*\*(\d+)\. ", re.MULTILINE)
#: A phase section heading, e.g. `## PHASE 1 · LAKEHOUSE — the priority`.
_SECTION = re.compile(r"^## (PHASE [123] · [^\n]+|FRONTEND[^\n]*|LOW PRIORITY[^\n]*)$", re.MULTILINE)
#: A row of the counts table: `| **1 · Lakehouse** (…) | 120 | 26 |`.
_TABLE_ROW = re.compile(r"^\| \*\*([^*]+)\*\*[^|]*\| (\d+) \| (\d+) \|$", re.MULTILINE)
_TOTAL = re.compile(r"^\*\*(\d+) open items\*\*", re.MULTILINE)
_FOCUS = re.compile(r"<!-- FOCUS:START -->(.*?)<!-- FOCUS:END -->", re.DOTALL)

#: Table label -> the section heading that carries its items. Keyed on the table's own labels so a
#: renamed phase fails here rather than silently counting zero.
_LABEL_TO_SECTION = {
    "1 · Lakehouse": "PHASE 1 · LAKEHOUSE",
    "1 · Cross-cutting": "PHASE 1 · CROSS-CUTTING",
    "2 · Compute": "PHASE 2 · COMPUTE",
    "3 · Controlplane": "PHASE 3 · CONTROLPLANE",
    "Frontend": "FRONTEND",
    "Low priority": "LOW PRIORITY",
}


def _text() -> str:
    return BACKLOG.read_text(encoding="utf-8")


def test_the_header_total_matches_the_rows() -> None:
    text = _text()
    stated = _TOTAL.search(text)
    assert stated, "the header states no total — `**N open items**` is how this file reports its size"

    counted = len(_ITEM.findall(text))
    assert int(stated.group(1)) == counted, f"header says {stated.group(1)} open items; {counted} are rendered below"


def test_every_phase_row_matches_the_items_under_its_heading() -> None:
    """A per-phase count is what tells the reader how much of the lakehouse is left, so it is the
    number most worth keeping honest — a total can stay right while two phases drift in opposite
    directions."""
    text = _text()
    bounds = [(m.start(), m.group(1)) for m in _SECTION.finditer(text)] + [(len(text), None)]
    per_section = {}
    for i, (start, name) in enumerate(bounds[:-1]):
        per_section[name] = len(_ITEM.findall(text[start : bounds[i + 1][0]]))

    rows = _TABLE_ROW.findall(text)
    assert len(rows) == len(_LABEL_TO_SECTION), f"the counts table has {len(rows)} rows, expected {len(_LABEL_TO_SECTION)}"

    for label, stated, _high in rows:
        prefix = _LABEL_TO_SECTION.get(label.strip())
        assert prefix, f"counts table row {label!r} names no known phase — rename the row or update this gate"
        matched = [name for name in per_section if name and name.startswith(prefix)]
        assert len(matched) == 1, f"{prefix} matches {matched} section headings, expected exactly one"
        assert int(stated) == per_section[matched[0]], (
            f"the counts table says {label.strip()} has {stated} items; {per_section[matched[0]]} are rendered under {matched[0]!r}"
        )


def test_item_numbers_are_continuous() -> None:
    """A gap means an item was deleted without renumbering; a repeat means one was pasted twice. Both
    make every count above unverifiable, which is the whole point of this file."""
    numbers = [int(n) for n in _ITEM.findall(_text())]
    assert numbers, "no items are rendered"
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"item numbering is not continuous 1..{len(numbers)} — first break at {next((i + 1 for i, n in enumerate(numbers) if n != i + 1), None)}"
    )


def test_the_focus_block_exists_and_stays_small() -> None:
    """The Stop hook injects this block verbatim on every stop. Its predecessor reached 23,362
    characters; 4,000 is the cap that file set for itself after that."""
    focus = _FOCUS.search(_text())
    assert focus, "no FOCUS:START/FOCUS:END block — the Stop hook would fall back to the first 3,000 characters"

    body = focus.group(1).strip()
    assert len(body) <= 4000, f"the FOCUS block is {len(body)} characters; it is injected on every stop and the cap is 4,000"
    assert "LAKEHOUSE" in body, "the FOCUS block does not name the current priority, so injecting it tells a reader nothing"
