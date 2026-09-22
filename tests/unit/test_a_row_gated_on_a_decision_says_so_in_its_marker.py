"""A row whose BODY says it needs a decision must carry the `**blocked:**` marker its header uses.

The register's "workable" count is re-derived from those markers and is the number that says how much
of the priority anyone can pick up without a ruling. A row that argues in its own prose that the
remaining step is an owner call, while carrying no marker, is counted as available work — so the number
reads optimistic and the miss is only found by someone opening the row and discovering they cannot
finish it. That happened twice on 2026-09-17, to [[LH-094]] (*"flipping it is an owner call, not a test
result"*) and [[LH-037]] (*"Both are decisions; neither is a patch"*), both by accident.

A RATCHET, NOT A BAN, and the reason is that the signal is prose and prose is ambiguous. Three rows
match the phrase set while being legitimately unmarked: a decision about TIMING rather than a blocker, a
row citing a ruling already MADE, and a row whose SECOND question is an owner call while its first half
is ordinary work. A gate that failed on those would be deleted within a week. So the known set is
recorded and only its GROWTH fails — which is exactly the shape the estate already uses for
`envFrom` exceptions and for secret-env delivery.

WHY IT IS THE BODY AND NOT A HAND-KEPT LIST: a list drifts. The phrases below are the ones the register
actually uses when it means "somebody must rule on this", collected from the rows that carry the marker
correctly.
"""

from __future__ import annotations

import pathlib
import re


REGISTER = pathlib.Path(__file__).resolve().parents[2] / "open_backlog_left_new.md"

#: How the register says "this needs a ruling". Collected from rows that carry the marker correctly
#: rather than invented, so a new row phrasing it a fourth way is a gap in this list, not a false pass.
_DECISION = re.compile(r"owner call|owner ruling|owner decides|owner decision|needs a ruling|is a decision|owner picks|an owner's", re.IGNORECASE)

#: Rows that match the phrase set and are CORRECTLY unmarked, each with the reason. A row leaves this
#: set by gaining a marker or by losing the phrase — never by being deleted to quiet the gate.
_MATCHES_BUT_NOT_GATED: dict[str, str] = {
    # A row is added here only with a sentence a reader can check, and every entry so far has left the
    # set the only way a row may: LH-034, LH-096, LH-159 and LH-164 by gaining a `**blocked:**` marker
    # or losing the incidental phrase, and LH-184 by closing.
    #
    # LH-055 matches on the TITLE OF THE RULING THAT UNBLOCKED IT. `docs/DECISIONS.md:2240` is headed
    # "A · The FGA model shape is a PORT, not a decision", and the row quotes it to explain why it is
    # no longer waiting on one. So the phrase argues the OPPOSITE of what this gate reads it as —
    # which is the one case the regex cannot distinguish and a reader can in a second.
    "LH-055": "quotes the ruling that unblocked it, whose title contains the word — 'a PORT, not a decision'",
}


def _phase_1_rows() -> list[tuple[str, str, bool]]:
    """(id, body, has_marker) for every OPEN phase-1 lakehouse row."""
    lines = REGISTER.read_text().split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("## PHASE 1 · LAKEHOUSE"))
    end = next(i for i, line in enumerate(lines) if line.startswith("## PHASE 1 · CROSS-CUTTING"))
    heads = [(i, m) for i in range(start, end) if (m := re.match(r"^\*\*(LH-\d+) · (.*)", lines[i]))]
    rows = []
    for k, (i, m) in enumerate(heads):
        stop = heads[k + 1][0] if k + 1 < len(heads) else end
        if m.group(2).startswith("~~"):  # struck through = closed
            continue
        body = "\n".join(lines[i:stop])
        rows.append((m.group(1), body, "**blocked:**" in body))
    return rows


def test_the_walk_sees_the_register() -> None:
    """Without this the ratchet would pass by measuring nothing."""
    rows = _phase_1_rows()
    assert len(rows) > 30, f"only {len(rows)} open phase-1 lakehouse rows parsed — the section headings or the row shape changed"


def test_no_NEW_row_argues_for_a_decision_without_carrying_the_marker() -> None:
    """The ratchet. It fails on a row that JOINS the mismarked set, never on the recorded four.

    If this is red: read the row. Either its remaining step really is a ruling — give it a
    `**blocked:**` marker naming the question — or the phrase is incidental and it belongs in
    `_MATCHES_BUT_NOT_GATED` with the reason, which is a sentence a reader can check.
    """
    offenders = sorted(rid for rid, body, marked in _phase_1_rows() if not marked and _DECISION.search(body) and rid not in _MATCHES_BUT_NOT_GATED)

    assert offenders == [], (
        f"{offenders} argue in their own body that the remaining step is a decision, but carry no `**blocked:**` marker — "
        "so the register counts them as work anyone can pick up. Add the marker with the question, or record the row in "
        "`_MATCHES_BUT_NOT_GATED` with why the phrase is incidental."
    )


def test_the_recorded_exceptions_are_still_real() -> None:
    """An exception that no longer matches is a stale silencer, and this is how it gets removed.

    Each recorded row must still EXIST, still lack a marker, and still match the phrase set. A row that
    gained a marker leaves the set; a row that lost the phrase leaves it too — either way the dict
    shrinks rather than accumulating reasons nobody re-reads.
    """
    by_id = {rid: (body, marked) for rid, body, marked in _phase_1_rows()}
    stale = []
    for rid in sorted(_MATCHES_BUT_NOT_GATED):
        if rid not in by_id:
            stale.append(f"{rid}: no longer an open phase-1 row")
        elif by_id[rid][1]:
            stale.append(f"{rid}: now carries a `**blocked:**` marker, so the exception is spent")
        elif not _DECISION.search(by_id[rid][0]):
            stale.append(f"{rid}: no longer matches the phrase set, so the exception is spent")

    assert stale == [], "these recorded exceptions are stale — drop them in this commit:\n  " + "\n  ".join(stale)
