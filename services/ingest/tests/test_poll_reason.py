"""A13 — the POLL REASON doctrine, extended from the frontend to this plane.

The estate already forbids unexplained timers in the JS plane: `zone-contract/src/poll-reason.test.ts`
took 13 timers down to 1-2 marked survivors. A13 says the same rule holds here, and until now it did
not exist at all — `POLL REASON` appeared in **zero** Python files while this plane had shipped a
`while True: await asyncio.sleep(...)` of its own.

WHY A GATE AND NOT A CONVENTION. A polling loop is the cheapest wrong answer to every distributed
question, and it is invisible in review because it *works*: it just costs a request per tick forever,
against a service that has no idea it is being asked. The ingest plane's whole design is the opposite
— JetStream's pull `fetch` is server-fulfilled, and Dapr suspends a workflow durably rather than
spinning. A timer that creeps in silently undoes that, and nothing else would notice.

WHAT THIS GATE ACTUALLY ENFORCES — narrower than "no timers", because two kinds are legitimate:

  POLLING          a loop that repeatedly ASKS for state ("is it done yet"). This is what A13
                   outlaws. It is what `ray_kit.await_success`'s `while True: sleep()` did inside an
                   HTTP request, and it does not survive into this plane in any form.
  KEEPALIVE        a loop that TELLS, on a schedule, and asks nothing — the ack heartbeat. Bounded
                   by the work it accompanies, not by an answer it is waiting for.
  BACKOFF          a sleep between bounded retry attempts. Not a loop waiting on state at all.

So the rule is: every in-process sleep is ANNOTATED with `POLL REASON:` naming which of these it is
and why nothing event-driven would do. Explicitly NOT counted (stated here so the gate is
implementable rather than aspirational): Dapr Workflow durable timers, which are runtime-managed and
never in-process; and the out-of-process ops CronJobs, which are §6c's carve-out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src" / "ingest"

#: A sleep is only interesting if it is IN-PROCESS and time-based. Matches the three spellings this
#: plane could plausibly use; a new one that dodges all three is a gap this test should be widened for
#: rather than a licence.
_SLEEP = re.compile(r"\b(?:await\s+asyncio\.sleep|asyncio\.sleep|time\.sleep)\s*\(")

#: The marker, matched case-sensitively and with the colon, so a passing mention in prose ("we do not
#: poll here") cannot satisfy it. The frontend gate uses exactly this token.
_MARKER = "POLL REASON:"

#: How far above the sleep the marker may sit. Generous enough for a docstring or a comment block,
#: tight enough that a marker attached to a different function cannot cover an unrelated timer.
_LOOKBACK_LINES = 30


def _sleeps_in(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [(n, line.strip()) for n, line in enumerate(lines, start=1) if _SLEEP.search(line)]


def _has_marker_above(path: Path, line_no: int) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = max(0, line_no - _LOOKBACK_LINES)
    return any(_MARKER in line for line in lines[start:line_no])


def test_every_in_process_sleep_in_the_ingest_plane_declares_its_REASON() -> None:
    """The gate itself. A new timer fails here until someone writes down why it must exist.

    The failure message names the file and the line, because "there is an unexplained timer somewhere
    in the plane" is not actionable and this is exactly the class of finding that gets deferred when
    it is vague.
    """
    unmarked = [
        f"{path.relative_to(SRC.parent.parent)}:{line_no}  {text}"
        for path in sorted(SRC.rglob("*.py"))
        for line_no, text in _sleeps_in(path)
        if not _has_marker_above(path, line_no)
    ]

    assert not unmarked, (
        "in-process sleep with no `POLL REASON:` above it — A13 forbids an unexplained timer in this plane.\n"
        "If it POLLS (asks for state on a schedule), it should not exist: JetStream's pull fetch is\n"
        "server-fulfilled and Dapr suspends workflows durably. If it is a keepalive or a retry backoff,\n"
        "say so in a `POLL REASON:` comment naming why nothing event-driven serves.\n  " + "\n  ".join(unmarked)
    )


def test_the_gate_FAILS_on_a_seeded_violation(tmp_path: Path) -> None:
    """A10's rule applied to this gate: a gate nobody has seen fail is a gate nobody should trust.

    The whole file above passes trivially if `_SLEEP` never matches anything — which is precisely how
    the estate's earlier grep-gates went quiet (the `iiif` gate matched a literal that had moved).
    So: plant a timer with no marker and prove the detector fires, then add the marker and prove it
    stops firing.
    """
    offender = tmp_path / "sneaky.py"
    offender.write_text("import asyncio\n\n\nasync def spin():\n    while True:\n        await asyncio.sleep(5)\n", encoding="utf-8")

    found = _sleeps_in(offender)
    assert found, "the sleep detector matched nothing — the gate is checking nothing"
    assert not _has_marker_above(offender, found[0][0]), "an unmarked timer was reported as marked"

    offender.write_text(
        "import asyncio\n\n\nasync def spin():\n    # POLL REASON: keepalive, not a poll — see the heartbeat.\n    while True:\n        await asyncio.sleep(5)\n",
        encoding="utf-8",
    )
    found = _sleeps_in(offender)
    assert _has_marker_above(offender, found[0][0]), "a marked timer was still reported as unmarked"


def test_the_marker_must_be_the_TOKEN_not_a_passing_mention(tmp_path: Path) -> None:
    """`POLL REASON:` with the colon, case-sensitive.

    Without this, a docstring saying "nothing here polls" would satisfy the gate — the annotation has
    to be a deliberate act, not a coincidence of vocabulary.
    """
    offender = tmp_path / "prose.py"
    offender.write_text(
        'import asyncio\n\n\nasync def spin():\n    """Nothing in this function polls, we promise."""\n    await asyncio.sleep(5)\n',
        encoding="utf-8",
    )

    found = _sleeps_in(offender)
    assert found
    assert not _has_marker_above(offender, found[0][0]), "prose about polling satisfied the marker check"


@pytest.mark.parametrize(
    "forbidden",
    [
        "ray_kit.await_success",
        "await_success",
    ],
)
def test_the_named_ANTI_PATTERN_did_not_survive_the_move(forbidden: str) -> None:
    """A13 names one function outright: today's only production `while True: sleep()`, held inside an
    HTTP request (`ray_kit/submit.py`). It "does not survive the move in any form" — so this asserts
    the ingest plane never grew a caller of it."""
    hits = [str(p) for p in SRC.rglob("*.py") if forbidden in p.read_text(encoding="utf-8")]

    assert not hits, f"{forbidden!r} reached the ingest plane — A13 forbids it in any form: {hits}"
