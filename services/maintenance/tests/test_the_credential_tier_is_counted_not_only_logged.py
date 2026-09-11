"""Which credential signed a rewrite has to be a SERIES, not an INFO line per dataset.

`write_options_for` picks between a table-scoped vended credential and the ambient process credential,
and says which on an `info` log. Measured on the live estate 2026-09-11, 600 records in one window:

    300  credential vending unavailable for <table> (401)
    300  write credential AMBIENT — nothing vended
      0  SCOPED

Every rewrite took the fallback and nothing anywhere carried a number an alert could fire on. The
condition had been true long enough to be quoted in a closed backlog row as evidence a DIFFERENT fix
was working, which is what a per-dataset INFO line costs: it is present, it is correct, and it reads as
routine.

THE SAME GAP IS ASKED FOR TWICE. LH-078's "Closes when" wants "the AMBIENT-vs-SCOPED split as a
counter/alert rather than only a log line", and LH-142's wants the fallback surfaced "so 'every rewrite
is ambient' cannot be the quiet state again". One counter answers both.

A COUNTER, NOT A REFUSAL, and that is deliberate. Refusing to maintain a dataset whose vend failed would
stop reclaiming disk across the estate the moment the catalog has a bad minute — the failure mode
`compaction_plane_unavailable_falling_back` already argues against one layer up. The signal is what was
missing, not the behaviour.

ONE SERIES WITH A `tier` ATTRIBUTE rather than two counters, because the number an operator wants is the
RATIO — "8 ambient of 285" is a posture and "8 ambient" alone is not. It also makes the all-ambient case
visible as a ratio hitting 1, which is exactly the state that went unnoticed.
"""

from __future__ import annotations

import inspect

from maintenance.core import metrics
from maintenance.services import credentials


def test_the_metrics_module_can_record_which_credential_signed_a_rewrite() -> None:
    """The recorder must exist and take the tier, or the call sites have nothing to call."""
    assert hasattr(metrics, "record_credential_tier"), "no recorder for the ambient-vs-scoped split"

    signature = inspect.signature(metrics.record_credential_tier)
    assert "tier" in signature.parameters, f"the recorder must carry WHICH tier: {signature}"


def test_both_credential_paths_are_counted() -> None:
    """THE GATE. A counter on one branch only measures the half that was never the problem."""
    body = inspect.getsource(credentials.write_options_for)

    assert body.count("record_credential_tier") >= 2, (
        "both the vended and the fallback path must record — counting only successes leaves the all-ambient state looking exactly like no traffic"
    )
    ambient_at = body.index("AMBIENT")
    scoped_at = body.index("SCOPED")
    assert "record_credential_tier" in body[:ambient_at] or "record_credential_tier" in body[ambient_at:scoped_at], (
        "the fallback branch must record before it returns"
    )


def test_recording_a_tier_does_not_raise() -> None:
    """Exercised rather than only inspected: a recorder that throws would break every sweep.

    The no-op meter a test process gets is enough — what is checked is that the call is well-formed
    against the real instrument, which is the part a source assertion cannot see.
    """
    metrics.record_credential_tier(tier="scoped")
    metrics.record_credential_tier(tier="ambient")
