"""Close items in `open_backlog_left.md`: remove them and re-derive the counts.

`uv run python scripts/backlog_close.py LH-005 LH-013 -m "already implemented"`.

IDS ARE STABLE AND ARE NEVER REUSED OR RENUMBERED. The first cut of this register numbered items
1..267 by POSITION, which made every id a moving target: closing item 5 renamed old 6 to 5, so
"work on 13" meant different things before and after a close and no reference survived a session.
Per-phase ids (`LH-`, `XC-`, `CP-`, `CTL-`, `FE-`, `LOW-`) are assigned once; closing one leaves
every other id exactly where it was. A gap in the sequence is the CORRECT residue of a closed item,
which is why the counts gate checks uniqueness rather than continuity.

The phase table at the top still has to agree with the rows, and
`tests/unit/test_the_backlog_counts_itself.py` fails if it drifts, so this re-derives it in the same
pass.

NOTHING IS ARCHIVED, deliberately. The owner's instruction when this register was created
(2026-09-10): *"I dont care about keep tracking what have been done."* A closed item leaves in the
commit that closed it, where the reasoning belongs; adding a "closed" section here would rebuild the
history this file was made to shed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


REGISTER = Path(__file__).resolve().parents[1] / "open_backlog_left.md"

_ITEM_START = re.compile(r"^\*\*([A-Z]+-\d+) · ", re.MULTILINE)
_SECTION = re.compile(r"^## (PHASE [123] · [^\n]+|FRONTEND[^\n]*|LOW PRIORITY[^\n]*)$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^(\| \*\*([^*]+)\*\*[^|]*\| )(\d+)( \| )(\d+)( \|)$", re.MULTILINE)
_TOTAL = re.compile(r"^\*\*(\d+) open items\*\*", re.MULTILINE)

_LABEL_TO_SECTION = {
    "1 · Lakehouse": "PHASE 1 · LAKEHOUSE",
    "1 · Cross-cutting": "PHASE 1 · CROSS-CUTTING",
    "2 · Compute": "PHASE 2 · COMPUTE",
    "3 · Controlplane": "PHASE 3 · CONTROLPLANE",
    "Frontend": "FRONTEND",
    "Low priority": "LOW PRIORITY",
}


def _blocks(text: str) -> list[tuple[str, int, int]]:
    """(id, start, end) for every rendered item, end-exclusive."""
    starts = [(m.group(1), m.start()) for m in _ITEM_START.finditer(text)]
    out = []
    for i, (num, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        out.append((num, start, end))
    return out


def _retotal(text: str) -> str:
    """Re-derive the phase table and the grand total from the rows that are actually present."""
    bounds = [(m.start(), m.group(1)) for m in _SECTION.finditer(text)] + [(len(text), None)]
    per_section: dict[str, tuple[int, int]] = {}
    for i, (start, name) in enumerate(bounds[:-1]):
        body = text[start : bounds[i + 1][0]]
        if name:
            per_section[name] = (len(_ITEM_START.findall(body)), len(re.findall(r"· \*\*HIGH\*\*", body)))

    def _row(m: re.Match[str]) -> str:
        prefix = _LABEL_TO_SECTION.get(m.group(2).strip())
        match = [k for k in per_section if prefix and k.startswith(prefix)]
        if len(match) != 1:
            return m.group(0)
        n, h = per_section[match[0]]
        return f"{m.group(1)}{n}{m.group(4)}{h}{m.group(6)}"

    text = _TABLE_ROW.sub(_row, text)
    return _TOTAL.sub(f"**{len(_ITEM_START.findall(text))} open items**", text, count=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Close backlog items by number.")
    ap.add_argument("ids", nargs="*", help="item ids to close, e.g. LH-005")
    ap.add_argument("--recount", action="store_true", help="re-derive the phase table without closing anything (after adding a row by hand)")
    ap.add_argument("-m", "--reason", default="", help="printed back, so the commit message can quote it")
    args = ap.parse_args()

    text = REGISTER.read_text(encoding="utf-8")
    blocks = _blocks(text)
    by_num = {n: (s, e) for n, s, e in blocks}

    if args.recount:
        # ADDING a row is the other half of draining, and it drifts the phase table exactly as closing
        # one does. Without this the only way to re-derive was to close something, so a hand-added row
        # left the counts gate red and invited a hand-edited total — the drift this file exists to end.
        if args.ids:
            print("!! --recount closes nothing; pass it alone")
            return 1
        REGISTER.write_text(_retotal(text), encoding="utf-8")
        print(f"phase table re-derived; {len(_ITEM_START.findall(text))} open items")
        return 0

    if not args.ids:
        print("!! name at least one item id to close, or pass --recount")
        return 1

    wanted = [i.upper() for i in args.ids]
    missing = [n for n in wanted if n not in by_num]
    if missing:
        print(f"!! no such item(s): {missing} — the register holds {len(blocks)} items")
        return 1

    closing = sorted(set(wanted))
    print(f"closing {len(closing)} item(s){': ' + args.reason if args.reason else ''}")
    for n in closing:
        s, _e = by_num[n]
        print(f"  {text[s:].split(chr(10))[0].strip('* ')[:130]}")

    # Cut from the END so earlier offsets stay valid. Surviving ids are untouched — see the module
    # docstring: a gap is the record that something closed, not a defect to tidy away.
    for n in sorted(closing, key=lambda i: by_num[i][0], reverse=True):
        s, e = by_num[n]
        text = text[:s] + text[e:]

    text = _retotal(text)
    total = len(_ITEM_START.findall(text))
    REGISTER.write_text(text, encoding="utf-8")
    print(f"\n{total} open items remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
