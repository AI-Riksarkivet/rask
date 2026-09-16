"""`open_backlog_left.md` states counts, and this re-derives them from the rows.

A register's header is the only part most readers read, and this estate has been bitten by headers
that disagreed with their own rows: `open_estate-verification.md` carried a context sentence its own
row 15 falsified, and `open_python-audit.md` reported OPEN counts that included corrections to its own
false claims. A count nobody can check is a claim, not a measurement.

Three shapes only, and each one has failed here before:

* the phase table's per-phase counts must match the items under each phase heading;
* item ids are UNIQUE. They are deliberately not continuous: ids are assigned once per phase
  (`LH-`, `XC-`, `CP-`, `CTL-`, `FE-`, `LOW-`) and never renumbered, so closing `LH-005` leaves a gap
  and leaves every other id where it was. The first cut numbered items 1..267 by position, which made
  every reference a moving target — "work on 13" meant different things either side of a close. A gap
  is therefore the correct residue of a closed item; a DUPLICATE is the real defect, because two rows
  answering to one id is how a closure silently removes the wrong one;
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

#: A rendered item: `**<PHASE>-<n> · <title>**` at the start of a line. The id is stable for the life
#: of the item — see the module docstring on why it is not positional. Counts EVERY row, closed ones
#: included, because two rows answering to one id is a problem whatever their state.
_ITEM = re.compile(r"^\*\*([A-Z]+-\d+) · ", re.MULTILINE)

#: An item that is still OPEN, with everything under its title up to the next item. A closed row stays
#: rendered — its measurements are why the row was worth keeping — and is struck through, so `~~` right
#: after the separator is the file's own marker for "done".
#:
#: THE SIZE OF THE FILE IS NOT THE SIZE OF THE WORK, and conflating them is how the header came to
#: overstate by 42 rows (273 claimed, 231 open, measured 2026-09-11). The number here is the one the
#: owner reads to decide what is left, so it counts what is left.
_OPEN_ITEM = re.compile(r"^\*\*[A-Z]+-\d+ · (?!~~)[^\n]*\n(.*?)(?=^\*\*[A-Z]+-\d+ · |\Z)", re.MULTILINE | re.DOTALL)

#: A row's metadata: backticked service tags, priority, and sometimes `**blocked:**`. IT IS FOUND BY
#: SCANNING, NOT BY TAKING THE LINE UNDER THE TITLE — measured 2026-09-16, 13 open rows carry a
#: multi-line RE-MEASURED block between the two, and reading line+1 scored those against prose. The
#: cost was not theoretical: the HIGH column it feeds said 11 for phase 1 when 16 rows were marked,
#: and 34 estate-wide against a true 39, so five HIGH lakehouse rows were invisible to the header that
#: decides what gets worked next.
_METADATA = re.compile(r"^`[^`\n]+`[^\n]*·[^\n]*$", re.MULTILINE)


def _metadata_of(body: str) -> str:
    """The row's metadata line, or "" for a row that renders none (which is itself a legible state —
    an unclassified row simply carries no priority)."""
    found = _METADATA.search(body)
    return found.group(0) if found else ""


#: A phase section heading, e.g. `## PHASE 1 · LAKEHOUSE — the priority`.
_SECTION = re.compile(r"^## (PHASE [123] · [^\n]+|FRONTEND[^\n]*|LOW PRIORITY[^\n]*)$", re.MULTILINE)
#: A row of the counts table: `| **1 · Lakehouse** (…) | 120 | 26 |`.
_TABLE_ROW = re.compile(r"^\| \*\*([^*]+)\*\*[^|]*\| (\d+) \| (\d+) \|$", re.MULTILINE)
_TOTAL = re.compile(r"^\*\*(\d+) open items\*\*", re.MULTILINE)
#: The sentence beside it: `A further 94 rows\nare CLOSED and still rendered`. Wraps mid-sentence in
#: the file, so the newline is part of the pattern rather than something to normalise away.
_CLOSED_TOTAL = re.compile(r"A further (\d+) rows\s+are CLOSED", re.MULTILINE)
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

    counted = len(_OPEN_ITEM.findall(text))
    assert int(stated.group(1)) == counted, (
        f"header says {stated.group(1)} open items; {counted} are still open below (closed rows stay rendered, struck through)"
    )


def test_the_header_count_of_CLOSED_rows_matches_the_struck_titles() -> None:
    """The half of the header that measures PROGRESS rather than what is left.

    It drifted where the open count could not: the open total has been gated since this file existed,
    so every closure corrected it — while the closed sentence beside it sat at 50 against 93 struck
    rows (measured 2026-09-16), understating the work done by 43 rows. A register whose progress number
    is stale argues for itself badly, which matters because this sentence is what someone reads to
    decide whether the drain is worth continuing.
    """
    text = _text()
    stated = _CLOSED_TOTAL.search(text)
    assert stated, "the header states no closed count — `A further N rows are CLOSED` is how this file reports progress"

    struck = len(re.findall(r"^\*\*[A-Z]+-\d+ · ~~", text, re.MULTILINE))
    assert int(stated.group(1)) == struck, f"header says {stated.group(1)} rows are closed; {struck} titles are struck through"

    assert struck + len(_OPEN_ITEM.findall(text)) == len(_ITEM.findall(text)), "open + closed does not account for every rendered row"


def test_every_phase_row_matches_the_items_under_its_heading() -> None:
    """A per-phase count is what tells the reader how much of the lakehouse is left, so it is the
    number most worth keeping honest — a total can stay right while two phases drift in opposite
    directions."""
    text = _text()
    bounds = [(m.start(), m.group(1)) for m in _SECTION.finditer(text)] + [(len(text), None)]
    per_section = {}
    for i, (start, name) in enumerate(bounds[:-1]):
        per_section[name] = [_metadata_of(body) for body in _OPEN_ITEM.findall(text[start : bounds[i + 1][0]])]

    rows = _TABLE_ROW.findall(text)
    assert len(rows) == len(_LABEL_TO_SECTION), f"the counts table has {len(rows)} rows, expected {len(_LABEL_TO_SECTION)}"

    for label, stated, stated_high in rows:
        prefix = _LABEL_TO_SECTION.get(label.strip())
        assert prefix, f"counts table row {label!r} names no known phase — rename the row or update this gate"
        matched = [name for name in per_section if name and name.startswith(prefix)]
        assert len(matched) == 1, f"{prefix} matches {matched} section headings, expected exactly one"
        rendered = per_section[matched[0]]
        assert int(stated) == len(rendered), f"the counts table says {label.strip()} has {stated} items; {len(rendered)} are still open under {matched[0]!r}"
        # The HIGH column was parsed and never checked, so it could drift freely while the row beside it
        # stayed honest — and it is the column that decides what gets worked next.
        high = sum(1 for metadata in rendered if "**HIGH**" in metadata)
        assert int(stated_high) == high, f"the counts table says {label.strip()} has {stated_high} HIGH; {high} open rows are marked **HIGH**"


def test_item_ids_are_unique() -> None:
    """Two rows answering to one id is how `scripts/backlog_close.py` removes the wrong one — it cuts
    by id, so a duplicate makes the closure ambiguous and silent. Gaps are fine and expected."""
    ids = _ITEM.findall(_text())
    assert ids, "no items are rendered"
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicated, f"these ids appear more than once, so closing one is ambiguous: {duplicated}"


def test_the_focus_block_exists_and_stays_small() -> None:
    """The Stop hook injects this block verbatim on every stop. Its predecessor reached 23,362
    characters; 4,000 is the cap that file set for itself after that."""
    focus = _FOCUS.search(_text())
    assert focus, "no FOCUS:START/FOCUS:END block — the Stop hook would fall back to the first 3,000 characters"

    body = focus.group(1).strip()
    assert len(body) <= 4000, f"the FOCUS block is {len(body)} characters; it is injected on every stop and the cap is 4,000"
    assert "LAKEHOUSE" in body, "the FOCUS block does not name the current priority, so injecting it tells a reader nothing"


#: How a row DECLARES itself finished. Deliberately narrow, and CASE-SENSITIVE on purpose: this file's
#: convention is that a closure verdict is shouted or emphasised, while ordinary prose about progress is
#: not. Matching case-insensitively flagged LH-075, an open row whose bullet says its doors are "done and
#: observed" while the half it tracks is still missing — a gate that cries wolf on open rows gets the
#: striking convention abandoned rather than followed.
#:
#: `Closes when:` is the OPEN-row convention and must never match here.
_DECLARES_CLOSED = re.compile(r"\*\*CLOSED|\*\*closed by measurement|\*Closed by:\*|CLOSED AND OBSERVED|DONE AND OBSERVED")


def _rows(text: str) -> list[str]:
    """Each item's full block: its title line through to the next item."""
    parts = re.split(r"^(\*\*[A-Z]+-\d+ · )", text, flags=re.MULTILINE)
    return [parts[i] + parts[i + 1] for i in range(1, len(parts) - 1, 2)]


def test_a_row_that_declares_itself_closed_is_struck_through() -> None:
    """The two ways a row says "done" have to agree, or the count believes the wrong one.

    Striking the title is what the counter reads; the body is what a human reads. Four rows had written
    the verdict in the body and left the title standing (LH-003, LH-084, LH-134, CP-009, found
    2026-09-11), so the file said 231 open while 227 were — and one of them, CP-009, had `Closes when:
    Strike the row` as its own instruction, unfollowed. A closed row still belongs in the file; what it
    must not do is keep counting.
    """
    offenders: list[str] = []
    for row in _rows(_text()):
        identifier = re.match(r"\*\*([A-Z]+-\d+) · ", row)
        heading = row.split("\n", 1)[0]
        if identifier and _DECLARES_CLOSED.search(row) and "~~" not in heading:
            offenders.append(identifier.group(1))
    assert not offenders, f"these rows declare themselves closed in the body but their titles are not struck, so they still count as open work: {offenders}"


def test_the_closer_and_this_gate_read_the_register_the_same_way() -> None:
    """`scripts/backlog_close.py` re-derives the header; this file checks it. Two readings, one file.

    They drifted, and the documented command was the casualty: the closer counted every RENDERED item
    and searched whole sections for `· **HIGH**`, so `--recount` over the register measured 2026-09-16
    would have written "302 open items" against 220 open, and 150 items / 26 HIGH for phase 1 against
    74 / 16 — closed rows counted as work left, and then the gate here would have failed the file the
    tool had just written. Running its re-derivation over an already-correct register must change
    nothing; anything else means one of the two has moved.
    """
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "backlog_close.py"
    spec = importlib.util.spec_from_file_location("backlog_close", path)
    assert spec and spec.loader, f"{path} is not importable, so the register's own closer cannot be checked"
    closer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(closer)

    text = _text()

    assert closer._retotal(text) == text, "scripts/backlog_close.py would rewrite the header of an already-correct register — the two readings have drifted"
