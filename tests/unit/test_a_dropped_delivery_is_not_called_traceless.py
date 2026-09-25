"""The estate must not claim a DROP is discarded while its subscriptions dead-letter one ([[LH-151]]).

**A DROP ROUTES TO THE DEAD-LETTER TOPIC** — `dapr/dapr#7097`, merged 2023-10-27 on issue #6282. It
acks, so the sidecar does not retry it, and it parks. Twelve places in this estate asserted the
opposite ("Dapr neither redelivers nor dead-letters"): `transform.py` (3x), `core/metrics.py` (2x),
`docs/DECISIONS.md`, `chart/alerting/rules.yml`, the Perses dashboard operators read, and four test
files. That is what this gate exists to keep out.

It is false HERE, not merely out of date upstream. Measured on the running pods 2026-09-19: all four
medallion subscribers carry `MEDALLION_DLQ_TOPIC=dlq.<appId>` — `rask-medallion-producer`,
`rask-bronze-to-silver`, `rask-media-to-silver`, `rask-silver-to-gold` — so every one of the eleven
deterministic `_drop(...)` sites in `transform.py` parks, and `MedallionCascadeDeadLettering` pages on
it as "a stage delivery gave up".

**WHY THIS IS A GATE AND NOT JUST A REWRITE.** The claim is what a reader uses to size the cost of a
DROP, and it was wrong in the expensive direction: it said nothing is retained when in fact everything
is parked AND paged. The Perses panel told an operator `drop` "is the one delivery outcome that leaves
no other trace" while that outcome was the one filling the DLQ. Prose that contradicts a measured fact
should fail a test, the same way `scripts/comment_history_gate.py` makes a rule enforceable rather
than remembered.

This gate goes away when the claim becomes true again — i.e. if a subscription ever drops its
`deadLetterTopic`, the sentence is correct for that lane and belongs there with the condition stated.
"""

from __future__ import annotations

import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]

#: The falsified sentence, matched loosely enough to catch a re-phrasing that keeps the claim.
_CLAIM = re.compile(r"neither\s+redelivers\s+nor\s+dead[- ]letters", re.IGNORECASE)

#: Where the estate's prose about delivery outcomes lives. The backlog is excluded: it RECORDS the
#: defect and must keep quoting it to stay readable.
_SEARCHED = ("services", "packages", "tests", "chart", "docs", "scripts")
_SUFFIXES = {".py", ".md", ".yaml", ".yml"}
_EXCLUDED_NAMES = {"open_backlog_left_new2.md", Path(__file__).name}


def _prose_files() -> list[Path]:
    out: list[Path] = []
    for d in _SEARCHED:
        root = _ROOT / d
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.suffix in _SUFFIXES and p.name not in _EXCLUDED_NAMES and ".venv" not in p.parts and "audits" not in p.parts:
                out.append(p)
    return out


def test_nothing_claims_a_drop_is_neither_redelivered_nor_dead_lettered() -> None:
    """The forward half. A DROP on a subscription that declares a dead-letter topic IS dead-lettered."""
    offenders = []
    for p in _prose_files():
        try:
            text = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _CLAIM.search(line):
                offenders.append(f"{p.relative_to(_ROOT)}:{n}")

    assert not offenders, (
        "these claim Dapr does not dead-letter a DROP, which dapr/dapr#7097 made false and every "
        "medallion subscriber disproves by declaring MEDALLION_DLQ_TOPIC:\n  " + "\n  ".join(offenders)
    )


def test_the_subscribers_that_make_it_false_still_declare_a_dead_letter_topic() -> None:
    """The backward half, and the reason this gate is honest rather than a style rule.

    The claim is false BECAUSE the subscriptions dead-letter. If that ever stops being true the gate
    above is forbidding a sentence that has become correct — so the premise is asserted here rather
    than assumed, and this test fails first, naming the reason.
    """
    chart = (_ROOT / "chart" / "templates" / "medallion.yaml").read_text()

    assert "MEDALLION_DLQ_TOPIC" in chart, "no medallion subscriber declares a dead-letter topic any more — re-read the gate above"
    assert chart.count("MEDALLION_DLQ_TOPIC") >= 2, "the producer and the stage runners both set it; one of the two is gone"
