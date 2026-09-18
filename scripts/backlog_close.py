"""Close items in `open_backlog_left_new.md`: remove them and re-derive the counts.

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


REGISTER = Path(__file__).resolve().parents[1] / "open_backlog_left_new.md"

_ITEM_START = re.compile(r"^\*\*([A-Z]+-\d+) · ", re.MULTILINE)

#: EVERY `##` heading, not only the six the table names — because that is how
#: `tests/unit/test_the_backlog_counts_itself.py` attributes a row, and the two readings have to be the
#: same one. Restricted to the six, this counted rows sitting under an intervening heading (`RIPE
#: DECISIONS`, `Dropped as ALREADY DONE`) as belonging to the phase above them, so the gate and the
#: writer disagreed by exactly those rows.
_SECTION = re.compile(r"^## (.+)$", re.MULTILINE)
#: `| **NAME** | open | workable | high |` — THREE counts. Written against a two-column table, this
#: matched no line at all, so `--recount` printed "phase table re-derived" and rewrote nothing;
#: the numbers stayed right only for as long as nothing moved. The label is the section heading, so
#: there is no name map to drift either.
_TABLE_ROW = re.compile(r"^\| \*\*(.+?)\*\* \| (\d+) \| (\d+) \| (\d+) \|$", re.MULTILINE)
#: All three numbers in the headline sentence, not just the first: a total that stays honest beside a
#: stale blocked count is the harder error to notice.
#: A row that needs a decision before anyone can finish it — on its OWN line, so a mention in prose is
#: not a gate. Same reading as the counts test.
_BLOCKED_LINE = re.compile(r"^- \*\*blocked:\*\*", re.MULTILINE)
_TOTAL = re.compile(r"^\*\*(\d+) open items\*\*, of which \*\*(\d+) are blocked on a decision\*\* and \*\*(\d+) can be picked up today\*\*", re.MULTILINE)


def _blocks(text: str) -> list[tuple[str, int, int]]:
    """(id, start, end) for every rendered item, end-exclusive."""
    starts = [(m.group(1), m.start()) for m in _ITEM_START.finditer(text)]
    out = []
    for i, (num, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        out.append((num, start, end))
    return out


def _walk(text: str) -> list[tuple[str, str]]:
    """(section, body) for every rendered row — THE GATE'S OWN READING, deliberately.

    A row's body runs to the next row or the next `##`, whichever comes first, and its section is the
    nearest heading above it. Deriving the counts any other way is how a writer and its gate end up
    disagreeing about the same file.
    """
    sections = [(m.start(), m.group(1).strip()) for m in _SECTION.finditer(text)]
    heads = list(_ITEM_START.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        nxt = next((pos for pos, _ in sections if pos > m.start()), len(text))
        out.append((next((n for pos, n in reversed(sections) if pos < m.start()), "?"), text[m.start() : min(end, nxt)]))
    return out


def _retotal(text: str) -> str:
    """Re-derive the phase table and the headline from the rows that are actually present."""
    rows = _walk(text)

    def _row(m: re.Match[str]) -> str:
        mine = [body for section, body in rows if section.startswith(m.group(1))]
        if not mine:
            return m.group(0)
        workable = sum(1 for body in mine if not _BLOCKED_LINE.search(body))
        high = sum(1 for body in mine if "**HIGH**" in body)
        return f"| **{m.group(1)}** | {len(mine)} | {workable} | {high} |"

    text = _TABLE_ROW.sub(_row, text)
    bodies = [body for _, body in _walk(text)]
    blocked = sum(1 for body in bodies if _BLOCKED_LINE.search(body))
    return _TOTAL.sub(
        f"**{len(bodies)} open items**, of which **{blocked} are blocked on a decision** and **{len(bodies) - blocked} can be picked up today**",
        text,
        count=1,
    )


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
        text = _retotal(text)
        REGISTER.write_text(text, encoding="utf-8")
        print(f"phase table re-derived; {len(_walk(text))} open items")
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
    total = len(_walk(text))
    REGISTER.write_text(text, encoding="utf-8")
    print(f"\n{total} open items remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
