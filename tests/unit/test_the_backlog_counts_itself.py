"""`open_backlog_left_new2.md` states counts, and this re-derives them from the rows.

A register's header is the only part most readers read, and this estate has been bitten by headers that
disagreed with their own rows: `open_estate-verification.md` carried a context sentence its own row 15
falsified, and `open_python-audit.md` reported OPEN counts that included corrections to its own false
claims. A count nobody can check is a claim, not a measurement.

Four shapes only, and each has failed here before:

* the section table's per-section counts must match the rows under each `## ` heading;
* the WORKABLE count must match the rows carrying no `blocked:` marker. That number is the one worth
  watching — it says how much of the priority anyone can pick up without a ruling — and it is the one
  that drifts, because a row can argue for a decision in its prose while carrying no marker;
* item ids are UNIQUE. They are deliberately not continuous: ids are assigned once per phase (`LH-`,
  `XC-`, `CP-`, `CTL-`, `FE-`, `LOW-`) and never renumbered, so closing `LH-005` leaves a gap and leaves
  every other id where it was. A gap is the correct residue of a closed item; a DUPLICATE is the real
  defect, because two rows answering to one id is how a closure silently removes the wrong one;
* the FOCUS block must exist and stay small: it is what a reader acts on first, and its predecessor grew
  to 23,362 characters by hoarding the record of the work it caused.

Deliberately NOT a whole-file lint. Everything else about the prose is a reader's job.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKLOG = ROOT / "open_backlog_left_new2.md"

#: A rendered row: `**<PHASE>-<n> · <title>**` at the start of a line.
_ITEM = re.compile(r"^\*\*([A-Z]+-\d+) · ", re.MULTILINE)
#: A row that needs a decision before anyone can finish it. Its own line, so a mention in prose does not
#: count — the marker is a claim the register makes, not a word that appears.
_BLOCKED = re.compile(r"^- \*\*blocked:\*\*", re.MULTILINE)
_SECTION = re.compile(r"^## (.+)$", re.MULTILINE)
#: `| **NAME** | open | workable | high |`
_TABLE_ROW = re.compile(r"^\| \*\*(.+?)\*\* \| (\d+) \| (\d+) \| (\d+) \|$", re.MULTILINE)
_HEADER_TOTAL = re.compile(r"\*\*(\d+) open items\*\*, of which \*\*(\d+) are blocked", re.DOTALL)


def _rows() -> list[tuple[str, str, str]]:
    """(id, section, body) for every row, in file order."""
    text = BACKLOG.read_text()
    sections = [(m.start(), m.group(1).strip()) for m in _SECTION.finditer(text)]
    heads = list(_ITEM.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        nxt = next((pos for pos, _ in sections if pos > m.start()), len(text))
        body = text[m.start() : min(end, nxt)]
        section = next((name for pos, name in reversed(sections) if pos < m.start()), "?")
        out.append((m.group(1), section, body))
    return out


def test_the_walk_sees_the_register() -> None:
    """Without this every assertion below would pass by measuring nothing."""
    assert len(_rows()) > 100, f"only {len(_rows())} rows parsed — the row shape or the headings changed"


def test_no_id_appears_twice() -> None:
    ids = [rid for rid, _, _ in _rows()]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"these ids are used by more than one row, so closing one would remove the wrong one: {dupes}"


def test_the_section_table_matches_the_rows_under_each_heading() -> None:
    rows = _rows()
    declared = {m.group(1): (int(m.group(2)), int(m.group(3)), int(m.group(4))) for m in _TABLE_ROW.finditer(BACKLOG.read_text())}
    assert declared, "no section table parsed — the header shape changed and these counts check nothing"

    wrong = []
    for name, (n_open, n_work, n_high) in declared.items():
        mine = [r for r in rows if r[1].startswith(name)]
        real_open = len(mine)
        real_work = sum(1 for _, _, body in mine if not _BLOCKED.search(body))
        real_high = sum(1 for _, _, body in mine if "**HIGH**" in body)
        if (real_open, real_work, real_high) != (n_open, n_work, n_high):
            wrong.append(f"{name}: table says {(n_open, n_work, n_high)}, rows say {(real_open, real_work, real_high)}")
    assert not wrong, "the section table disagrees with its own rows:\n  " + "\n  ".join(wrong)


def test_the_headline_total_matches_the_rows() -> None:
    """The two numbers a reader actually quotes."""
    rows = _rows()
    m = _HEADER_TOTAL.search(BACKLOG.read_text())
    assert m, "the header no longer states an open/blocked total — that sentence is what most readers quote"
    blocked = sum(1 for _, _, body in rows if _BLOCKED.search(body))
    assert (int(m.group(1)), int(m.group(2))) == (len(rows), blocked), (
        f"header says {m.group(1)} open / {m.group(2)} blocked; the rows say {len(rows)} / {blocked}"
    )


def test_the_focus_block_exists_and_stays_small() -> None:
    """The cap is the point: its predecessor reached 23,362 characters by hoarding the record of the work it caused."""
    block = re.search(r"<!-- FOCUS:START -->(.*?)<!-- FOCUS:END -->", BACKLOG.read_text(), re.DOTALL)
    assert block, f"{BACKLOG.name} carries no FOCUS block"
    assert len(block.group(1)) < 6000, f"{BACKLOG.name}'s FOCUS block is {len(block.group(1))} chars"


def test_no_row_header_is_SWALLOWED_by_the_line_above_it() -> None:
    """A header sharing a line with the previous row's body is invisible to every reader here.

    MEASURED 2026-09-20: closing XC-034 joined `**XC-036 · …**` onto the end of the closure note, and
    XC-036 vanished from the register — not from the FILE, from every parser of it. Both readers anchor
    on `^\\*\\*<ID> · ` (this module and `scripts/backlog_close.py`), so the row parsed as body text of a
    CLOSED row.

    THE COUNT GATE ABOVE CANNOT CATCH THIS, which is why the check is written separately. The table
    stayed green because two errors cancelled exactly: a closed row still occupying a slot, and an open
    row not occupying one. Arithmetic neutrality is precisely the condition under which a counting gate
    reports health about the thing it is blind to.

    AND IT IS A DATA-LOSS PATH, not only a reporting one: `backlog_close.py` cuts header-to-next-header,
    so closing the row above would have deleted the swallowed row's entire body with it.
    """
    text = BACKLOG.read_text(encoding="utf-8")
    swallowed = [
        line[:90]
        for line in text.splitlines()
        # A header ANYWHERE but column 0 on a line that has other content before it.
        if re.search(r"\S.*\*\*[A-Z]+-\d+ · ", line) and not line.startswith("**")
    ]

    assert swallowed == [], "these row headers share a line with other text, so no parser here can see them:\n  " + "\n  ".join(swallowed)


def test_every_row_states_how_it_closes_and_what_it_rests_on() -> None:
    """A row with no `Closes when` is one nobody can ever verify as done.

    FOUND 2026-09-21 AND THIS IS WHY IT IS A GATE. `XC-039` carried none, and the absence was not
    merely untidy — it actively misled. An `awk '/XC-039/,/Closes when/'` range run against it does not
    match nothing; a range whose terminator is missing runs to the NEXT occurrence, so it printed
    `XC-042`'s closing bar as though it were XC-039's. Two rows then read as identical, and the edit
    built on that reading overwrote the wrong row. The tool answered honestly; the attribution was the
    error, and nothing in the output said which row the line came from.

    `Evidence` is checked in the same pass for the same reason one level down: a claim with no citation
    cannot be re-measured, and this register's standing rule is that a verdict is not evidence it is
    still true. Both fields are what make a row falsifiable by the next reader.

    Bounded by the NEXT ROW OR THE NEXT `## ` HEADING, whichever comes first — the same reading
    `_walk` and `backlog_close.py` use. Cutting only to the next row would let a section's last item
    absorb everything after it and pass on a neighbour's fields, which is the very confusion this
    test exists to catch.
    """
    text = BACKLOG.read_text()
    starts = [(m.group(1), m.start()) for m in re.finditer(r"^\*\*([A-Z]+-\d+) · ", text, re.MULTILINE)]
    headings = [m.start() for m in re.finditer(r"^## ", text, re.MULTILINE)]

    missing: dict[str, list[str]] = {"Closes when": [], "Evidence": []}
    for index, (row_id, start) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else len(text)
        body = text[start : min(end, next((h for h in headings if h > start), len(text)))]
        for field in missing:
            if f"- *{field}:*" not in body:
                missing[field].append(row_id)

    assert not missing["Closes when"], f"rows with no closing bar, so nothing can retire them: {missing['Closes when']}"
    assert not missing["Evidence"], f"rows with no evidence, so nothing can re-measure them: {missing['Evidence']}"
