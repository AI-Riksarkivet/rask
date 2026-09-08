"""When two release stores both hold the release, choosing one is not the wrapper's call.

`scripts/helm.sh` exists because the rask release lives in Postgres rather than a Kubernetes Secret,
and its header states the hazard it guards: an invocation against the wrong backend "concludes the
release is absent and INSTALLS OVER A LIVE ESTATE... a silent success against the wrong backend is
worse than any error."

Its rule for picking a driver was *"a store with releases in it is the store in use"*, which is sound
only while exactly ONE store has them. MEASURED 2026-09-08 on this estate:

    SQL / Postgres   rask rev 42   Aug 28
    Secret store     rask rev 108  Sep 7   (sh.helm.release.v1.rask.v101, written 24 h earlier)

so the probe selected a history ten days stale, and an `upgrade` through the wrapper would have
rewritten the whole fleet from that manifest — the same harm the header describes, reached from the
other direction.

A READ still answers and says which store it came from: refusing to answer would make a divergence
harder to diagnose than it already is. A MUTATION refuses, because only a person knows which history
is real.
"""

from __future__ import annotations

import re
from pathlib import Path


WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "helm.sh"

#: The subcommands that WRITE the release store. Named rather than inferred from a verb list, because
#: a new mutating subcommand must be a deliberate addition here, not something a regex waves through.
_MUTATING = ("upgrade", "install", "uninstall", "delete", "rollback")


def test_the_wrapper_refuses_to_mutate_when_both_stores_hold_a_release() -> None:
    body = WRAPPER.read_text()

    assert "SECRET_RELEASES=" in body, "the wrapper no longer probes the Secret store, so it cannot see a divergence"
    guard = re.search(r"if \[\[ -n \"\$SECRET_RELEASES\" \]\]; then(.+?)\nfi\n", body, re.DOTALL)
    assert guard, "the both-stores-populated guard is gone"
    for sub in _MUTATING:
        assert sub in guard.group(1), f"{sub!r} can still mutate an ambiguous release store"
    assert "exit 3" in guard.group(1), "the guard warns but does not stop, which is the failure it exists to prevent"


def test_only_MUTATING_verbs_are_refused_and_a_read_still_answers() -> None:
    """The other half. A divergence is already hard to diagnose; a wrapper that also refused to report
    either history would make it harder, so the gate lists exactly the verbs that WRITE."""
    body = WRAPPER.read_text()
    guard = re.search(r"if \[\[ -n \"\$SECRET_RELEASES\" \]\]; then(.+?)\nfi\n", body, re.DOTALL)
    assert guard

    pattern = re.search(r"case \"\$\{1:-\}\" in\s*\n\s*([a-z|]+)\)", guard.group(1))
    assert pattern, "the refusal is no longer a case over named subcommands"
    gated = set(pattern.group(1).split("|"))

    assert gated == set(_MUTATING), f"the gated set drifted from the verbs that write the store: {sorted(gated)}"
    assert "reading from SQL" in guard.group(1), "a read must say WHICH store answered when the stores disagree"
