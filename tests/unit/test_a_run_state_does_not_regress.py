"""No property of a run may be written last-delivery-wins.

the lakehouse register, row E3 (drained 2026-09-10; in git history) ("run state regresses on out-of-order ingest").

The BEHAVIOUR is proven against real AGE by
`tests/e2e-py/test_lineage_e2e.py::test_a_run_state_is_decided_by_event_time_not_by_delivery_order`,
which replays the shape measured on the deployed graph: the cascade's `derive_media` run took a FAIL
stamped 13:30:31 and then a COMPLETE stamped 13:29:50 — older, delivered second — and the graph
reported the failed run as succeeded. That test needs a live Apache AGE and SKIPS without one, so it
cannot be the only thing standing between this estate and the next unguarded property.

THIS FILE IS THE DRIFT GATE, and it is DERIVED rather than literal: it reads `MERGE_RUN`'s own source
and demands that every property it assigns is either guarded by a conditional or NAMED here with a
reason. A new `r.foo=$foo` fails here — which is how the defect arrived in the first place, one
reasonable-looking assignment at a time beside six that had already been made conditional.

It also pins the two definitions of "terminal" together. `cypher._TERMINAL` decides what the graph
refuses to regress out of; `postgres.TERMINAL_TYPES` decides what the durable feed dedups to one row
per run. They are the same notion — *a run has at most one of these* — and they are spelled twice
because one is Cypher and one is SQL, so nothing but a test keeps them equal.
"""

from __future__ import annotations

import re
from typing import Final

from lineage.services import cypher as cy
from lineage.services import postgres as pg


#: `r.<prop>=` and whatever begins its right-hand side, read out of the statement itself.
_ASSIGNMENT = re.compile(r"r\.(?P<prop>[a-z_]+)=(?P<rhs>\(?[A-Za-z$]+)")

#: The only properties allowed a bare `$param` right-hand side, each because a redelivery cannot make
#: it wrong. `job` is the run's denormalised `<namespace>/<name>` — one value for every event of a run,
#: so an older event writes exactly what a newer one would. `events_count` counts DELIVERIES, and a
#: superseded event was still delivered; gating it would hide the redelivery storms the guard exists to
#: survive.
_MAY_BE_UNCONDITIONAL: Final = frozenset({"job", "events_count"})

#: The lifecycle facts a stale event must not be able to restate. Each has to name `supersedes` — a
#: bare CASE on something else would satisfy "is conditional" while still regressing the run.
_STATE_FAMILY: Final = frozenset({"event_type", "event_time", "author", "producer", "error_message"})


def _assignments() -> dict[str, str]:
    return {m.group("prop"): m.group("rhs") for m in _ASSIGNMENT.finditer(cy.MERGE_RUN)}


def test_the_extraction_still_finds_the_statement() -> None:
    """The gate's own precondition: an assignment shape this file cannot parse makes it vacuous."""
    found = _assignments()
    assert len(found) >= 10, f"MERGE_RUN parsed to {len(found)} assignments — the extraction has drifted from the source: {found}"


def test_no_run_property_is_written_last_delivery_wins() -> None:
    """The headline: delivery order must not decide what a run says it did."""
    unconditional = sorted(prop for prop, rhs in _assignments().items() if rhs.startswith("$") and prop not in _MAY_BE_UNCONDITIONAL)
    assert not unconditional, (
        f"these Run properties are assigned straight from an event parameter: {unconditional}. Delivery is "
        "at-least-once and unordered, so the last event to ARRIVE is not the last event to HAPPEN — an "
        "older redelivery would overwrite a newer fact. Guard it on `supersedes`, or add it to "
        "_MAY_BE_UNCONDITIONAL with the reason a stale event cannot make it wrong"
    )


def test_the_state_family_is_guarded_on_supersedes_specifically() -> None:
    """Being conditional is not enough — the condition has to be the recency-and-terminal one."""
    body = cy.MERGE_RUN
    for prop in sorted(_STATE_FAMILY):
        assert f"r.{prop}=(CASE WHEN supersedes THEN" in body, f"Run.{prop} is not gated on `supersedes`: an out-of-order event can still rewrite it"


def test_a_terminal_state_is_sticky_against_a_non_terminal_one() -> None:
    """The row's own close condition, read off the predicate: a START arriving after a COMPLETE carries
    a FRESHER stamp, so the time half lets it through and only this clause stops it."""
    assert f"NOT (r.event_type IN {cy._TERMINAL} AND NOT $et IN {cy._TERMINAL})" in cy.MERGE_RUN, (
        "the terminal-stickiness clause is gone from MERGE_RUN — a late START would regress a finished run"
    )


def test_started_at_is_the_earliest_time_seen_not_the_first_delivered() -> None:
    """`coalesce(r.started_at, $tm)` recorded whichever event ARRIVED first, which on the measured run
    put the start 41 seconds after the finish."""
    assert "r.started_at=(CASE WHEN r.started_at IS NULL OR $tm < r.started_at THEN $tm" in cy.MERGE_RUN


def test_both_dialects_agree_on_what_terminal_means() -> None:
    """One notion, spelled once in Cypher and once in SQL — nothing but this keeps them equal."""
    cypher_states = sorted(re.findall(r"'([A-Z]+)'", cy._TERMINAL))
    sql_states = sorted(re.findall(r"'([A-Z]+)'", pg.TERMINAL_TYPES))
    assert cypher_states == sql_states, (
        f"cypher._TERMINAL {cypher_states} and postgres.TERMINAL_TYPES {sql_states} disagree about which "
        "run states are terminal — the graph would refuse to leave a state the feed still dedups, or worse"
    )
    assert cypher_states, "neither definition parsed — the gate would pass vacuously"


def test_the_attempt_counter_counts_FAILURES_and_not_every_event() -> None:
    """[[LH-098]]. `attempts` answers "did this run fail first, and how often" on the ONE node the
    deterministic-run-id flood guard makes every tick MERGE onto — so it must move only on a FAIL. An
    unguarded increment would count COMPLETEs and RECONCILEDs too and report a healthy run as a
    repeatedly-failing one.
    """
    assert "attempts" in _assignments(), "MERGE_RUN no longer assigns `attempts` — failures are structurally uncountable again"
    # Asserted against the STATEMENT, not the parsed rhs: `_ASSIGNMENT`'s rhs group captures only the
    # first token, because its question is "conditional or bare $param". Content checks in this file
    # read `cy.MERGE_RUN` directly, as the two guards above do.
    assert "r.attempts=(CASE WHEN $et = 'FAIL'" in cy.MERGE_RUN, (
        f"the attempt counter is not gated on a FAIL event, so every COMPLETE would increment it: {cy.MERGE_RUN}"
    )


def test_the_attempt_counter_cannot_be_inflated_by_a_REDELIVERY() -> None:
    """The correctness of the counter, and the reason it is not a bare `coalesce(...)+1`.

    `MERGE_RUN` runs on EVERY ingest, while `/events` dedups redelivered FAIL rows on its partial-unique
    `(run_id, event_type)` index. So an unguarded increment counts DELIVERIES rather than attempts, and
    the sidecar's own retry schedule would inflate it on its own — the number would measure the
    transport, not the dataset. A genuine attempt stamps a later `event_time`; a redelivery replays the
    same payload with the same one, so the strict comparison counts the first and ignores the rest.
    """
    body = cy.MERGE_RUN

    assert "r.attempts=(CASE WHEN $et = 'FAIL' AND coalesce(r.last_attempt_at, '') < $tm" in body, (
        "the attempt counter is not guarded on a strictly newer timestamp, so a redelivery increments it"
    )
    # The stamp must advance under the SAME condition, or the guard leaks: a counter that increments
    # without moving its watermark counts every later delivery too.
    assert "r.last_attempt_at=(CASE WHEN $et = 'FAIL' AND coalesce(r.last_attempt_at, '') < $tm" in body, (
        "`last_attempt_at` does not advance under the counter's own condition, so the guard cannot hold next time"
    )
