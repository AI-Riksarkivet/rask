"""The lakehouse backlog's header states counts it can be checked against.

A register's header is the only part most readers read, and this estate has been bitten by three
headers that disagreed with their own rows: `open_estate-verification.md` carried a context sentence
its row 15 falsified, `open_python-audit.md` reported OPEN counts that included corrections to its own
false claims, and this file claimed a freshness date two days older than rows struck beneath it while
naming three stale rows when twelve were closed.

So the header asserts numbers and this gate re-derives them. Not to police tidiness — to keep the file
usable as evidence. A count nobody can check is a claim, and the campaign that produced these files was
faulted for exactly that: *"Status is counted from this file, never asserted elsewhere."*

Deliberately NOT a whole-file lint. Two shapes only: the header's totals must match the rows, and a
row struck as done must not also be listed as open. Everything else about the prose is a reader's job.
"""

from __future__ import annotations

import re
from pathlib import Path


BACKLOG = Path(__file__).resolve().parents[2] / "open_lakehouse_diff_left.md"

#: A lettered work row — `### A1 · …`, struck when landed. The Q sections use a table instead: those
#: rows were carried in bulk from drained registers, or recorded in bulk from one live run, and each
#: is a line rather than a discussion. Q-numbered sections are matched by shape (`Q<n>-<n>`) rather
#: than by a list, so a new one is counted the day it is added — this gate shipped counting Q2 and Q3
#: only, and Q4 landed a day later.
_ROW = re.compile(r"^### (~~)?([A-Z]+\d*)[ ·]", re.MULTILINE)
#: Matched by SHAPE (`Q<n>-<n>`) rather than by a list of sections, so a new one is counted the day it
#: is added — this gate shipped counting Q2 and Q3 only, and Q4 landed a day later. A carried row can
#: be struck too: Q4 repaired two of its own findings in the commit that recorded them.
_CARRIED = re.compile(r"^\| (~~)?(Q\d+-\d+)~{0,2} \|", re.MULTILINE)
_HEADER = re.compile(r"\*\*Counted [0-9-]+, from the rows below rather than asserted: (\d+) tracked, (\d+) open, (\d+) struck\.\*\*")


def _text() -> str:
    return BACKLOG.read_text()


def test_the_header_totals_match_the_rows() -> None:
    text = _text()
    stated = _HEADER.search(text)
    assert stated, "the header no longer states counts in the checkable form — restore it or this gate is decoration"
    tracked, open_, struck = (int(g) for g in stated.groups())

    rows = _ROW.findall(text)
    carried = _CARRIED.findall(text)
    # A carried row can be struck too — Q4 repaired two of its own findings in the commit that
    # recorded them, and a register that could only mark a LETTERED row done would have to lie about
    # those or leave them out.
    real_struck = sum(1 for mark, _ in rows if mark) + sum(1 for mark, _ in carried if mark)
    real_tracked = len(rows) + len(carried)
    real_open = real_tracked - real_struck

    assert (tracked, open_, struck) == (real_tracked, real_open, real_struck), (
        f"header says {tracked} tracked / {open_} open / {struck} struck; "
        f"the rows say {real_tracked} / {real_open} / {real_struck}"
    )


def test_no_row_is_both_struck_and_open() -> None:
    """A struck heading means landed. Carrying the same id as an open row too is how a reader is told
    both things at once and believes whichever they read first."""
    rows = _ROW.findall(_text())
    struck = {rid for mark, rid in rows if mark}
    unstruck = {rid for mark, rid in rows if not mark}
    both = sorted(struck & unstruck)
    assert not both, f"ids listed as both struck and open: {both}"
