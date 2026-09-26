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


REGISTER = pathlib.Path(__file__).resolve().parents[2] / "open_backlog_left_new2.md"

#: How the register says "this needs a ruling". Collected from rows that carry the marker correctly
#: rather than invented, so a new row phrasing it a fourth way is a gap in this list, not a false pass.
_DECISION = re.compile(
    r"owner call|owner ruling|owner decides|owner decision|needs a ruling|is a decision|owner picks|an owner's"
    # ADDED 2026-09-23 after this list MISSED one, which is the failure the comment above predicts.
    # LH-076's remaining step opened "Do nothing until the ruling lands" and it matched nothing here, so
    # the row read as pickable and was picked — the gate was green and the register was wrong. These are
    # the phrasings that row actually used, collected the same way: from the text, not invented.
    r"|until the ruling|do nothing until|awaiting a ruling|not mine to pick|only the owner can",
    re.IGNORECASE,
)

#: Rows that match the phrase set and are CORRECTLY unmarked, each with the reason. A row leaves this
#: set by gaining a marker or by losing the phrase — never by being deleted to quiet the gate.
_MATCHES_BUT_NOT_GATED: dict[str, str] = {
    "LH-141": "the whole row is startable (admit restamp at the lineage door, then the one-shot repair door); the match is prose, not a pending ruling",
    "LH-262": "the kms statement and its unit test are startable after LH-177's split; the body says no owner ruling parks it",
    # A row is added here only with a sentence a reader can check, and every entry so far has left the
    # set the only way a row may: LH-034, LH-096, LH-159 and LH-164 by gaining a `**blocked:**` marker
    # or losing the incidental phrase, and LH-184 and LH-055 by closing.
    #
    # EMPTY, and LH-164 left it the way this set says a row may: by gaining a marker. It was recorded
    # here for arguing the opposite ("Registering it as a namespace is not a decision someone has been
    # putting off"), which was true of the NAMESPACE question `config.py:516-522` answered. A different
    # question outlived it — which of two homes each tier keeps, where its own clause says "Reaping the
    # wrong one destroys live rows" — and that one is a ruling. An exemption is only ever about the
    # question a row was carrying when it was written.
}


def _phase_1_rows() -> list[tuple[str, str, bool]]:
    """(id, body, has_marker) for every OPEN phase-1 lakehouse row."""
    lines = REGISTER.read_text().split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("## PHASE 1 · LAKEHOUSE"))
    end = next(i for i, line in enumerate(lines) if line.startswith("## PHASE 1 · CROSS-CUTTING"))
    # EVERY ID PREFIX, not just `LH-`. The lakehouse section also carries `LIN-`, `ZT-` and `CAT-` rows,
    # and a walk anchored on `LH-` made each of them invisible to this ratchet — a decision-gated row
    # could carry the wrong marker for ever as long as its id did not start with those two letters.
    # Found because this walk answered 30 where the mechanically-derived counts table said 31.
    heads = [(i, m) for i in range(start, end) if (m := re.match(r"^\*\*([A-Z]+-\d+) · (.*)", lines[i]))]
    rows = []
    for k, (i, m) in enumerate(heads):
        stop = heads[k + 1][0] if k + 1 < len(heads) else end
        if m.group(2).startswith("~~"):  # struck through = closed
            continue
        body = "\n".join(lines[i:stop])
        rows.append((m.group(1), body, "**blocked:**" in body))
    return rows


def test_the_walk_sees_the_register() -> None:
    """Without this the ratchet would pass by measuring nothing.

    CHECKED AGAINST THE REGISTER'S OWN COUNT, not a floor. A literal threshold makes closing rows fail
    this file — it read `> 30` and went red the day the section reached 30, which turns a vacuity guard
    into a brake on the work it is meant to protect. The counts table is derived mechanically by
    `test_the_backlog_counts_itself`, so agreeing with it proves the walk sees the same rows without
    anyone maintaining a number here.
    """
    rows = _phase_1_rows()
    declared = re.search(r"\| \*\*PHASE 1 · LAKEHOUSE\*\* \| (\d+) \|", REGISTER.read_text())
    assert declared, "the counts table no longer names PHASE 1 · LAKEHOUSE — this walk cannot be checked"
    assert len(rows) == int(declared.group(1)), (
        f"walked {len(rows)} open phase-1 lakehouse rows, the counts table declares {declared.group(1)} — the section headings or the row shape changed"
    )


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


#: A RULING, not the noun "rule". `\brule\b` matches "alert rule" and "key the rule on a statistic",
#: which is how a first draft of this flagged LH-050 and XC-067 for saying nothing of the kind.
_RULING = re.compile(r"\brul(?:ed|ing|ings)\b|\bdecision\b|\bdecides?\b", re.IGNORECASE)

#: The clause where a row says what REMAINS, which is the only place the distinction lives.
_WHAT_IS_LEFT = re.compile(r"^- \*What is left:\*(.*?)(?=^- \*|\Z)", re.MULTILINE | re.DOTALL)

#: Rows whose *What is left* mentions a ruling and are CORRECTLY unmarked, each with a checkable reason.
_RULING_IN_WHAT_IS_LEFT_BUT_STARTABLE: dict[str, str] = {
    "LH-099": "the 'decision record' is a DECISIONS.md entry written after observing one live compaction RunEvent; no owner ruling is pending",
    "LH-177": "'Workable now, whatever D9 decides': split the STS-call endpoint from the client-facing endpoint",
    "LH-204": "'decides' describes purge's code ('purge decides liveness by object id'), not a pending ruling; the register guard is startable",
    "LH-228": "'Workable now': require_no_live_trash fails closed, undrop compares locations, a trashed drop answers its transaction; only the expiry half waits",
    "LH-262": "'Workable now: the kms statement and its unit test'; the AWS proof waits for an AWS estate, not for a ruling",
    # The second row to match by DENYING it needs a ruling, which is now two of two — a phrase match
    # cannot read a negation, and rewording the row to dodge the gate would be worse than recording why
    # the gate does not apply.
    "LH-196": "says 'No ruling needed either way; this is test fidelity, not platform policy' — the e2e bypasses a seam production uses, and fixing the TEST needs nobody's permission",
    "LH-220": "'Implement D1', which is ruled; the 2026-09-26 ruling it cites only keeps the shared token unscoped until D1 lands",
    "LH-263": "cites the 2026-09-26 erasure ruling already made (a branch rewrite may copy what it inherits, 64 MiB cap); recording it in DECISIONS.md and every listed surface are startable",
}


def test_no_row_whose_REMAINING_STEP_is_a_ruling_goes_unmarked() -> None:
    """The phrase list above kept missing rows, so this checks the CLAUSE instead of the whole body.

    THREE ROWS IN ONE DAY said their remaining step was a ruling while carrying no marker — LH-076
    ("Do nothing until the ruling lands"), LH-141 ("needs a ruling") and LH-164 ("Obtain the ruling
    above"). Only the second matched `_DECISION`. Widening that list caught LH-076 and still missed
    LH-164, which is the signal that collecting phrasings does not converge: there is always another
    way to write it.

    WHY THE CLAUSE AND NOT THE BODY. A row citing a ruling that already LANDED is reporting a fact --
    `_RULING` over the whole body flags nine Phase-1 rows and most are exactly that. What a row says in
    *What is left* is different in kind: it is the claim the workable count is computed from.

    If this is red: read the clause. Either the remaining step really is a ruling — give it a
    `**blocked:**` marker naming the question — or the row has a startable arm as well, and belongs in
    `_RULING_IN_WHAT_IS_LEFT_BUT_STARTABLE` with the sentence that says so.
    """
    offenders = []
    for rid, body, marked in _phase_1_rows():
        if marked or rid in _RULING_IN_WHAT_IS_LEFT_BUT_STARTABLE:
            continue
        clause = _WHAT_IS_LEFT.search(body)
        if clause and _RULING.search(clause.group(1)):
            offenders.append(rid)

    assert sorted(offenders) == [], (
        f"{sorted(offenders)} say in *What is left* that the remaining step is a ruling, but carry no "
        "`**blocked:**` marker — so the register counts them as work anyone can pick up. Add the marker "
        "with the question, or record the row in `_RULING_IN_WHAT_IS_LEFT_BUT_STARTABLE` with the arm "
        "that IS startable."
    )
