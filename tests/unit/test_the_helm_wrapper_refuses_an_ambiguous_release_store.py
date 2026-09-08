"""When two release stores both hold the release, the wrapper must use the one the owner named — and say so.

`scripts/helm.sh` exists because helm embeds the whole chart in every revision, and on 2026-08-15 the
release SECRET crossed Kubernetes' 1 MiB object limit so nothing could be stored; the release moved to
Postgres. Its header states the hazard: an invocation against the wrong backend "concludes the release
is absent and INSTALLS OVER A LIVE ESTATE... a silent success against the wrong backend is worse than
any error."

Its rule for picking a driver was *"a store with releases in it is the store in use"*, which is sound
only while exactly ONE store has them. MEASURED 2026-09-08 on this estate:

    SQL / Postgres   rask rev 42   Aug 28
    Secret store     rask rev 108  Sep 7

so the probe selected a history ten days stale, and an `upgrade` through the wrapper would have
rewritten the whole fleet from that manifest — the same harm the header describes, reached from the
other direction.

OWNER RULING 2026-09-08: the SECRET store is authoritative and the SQL rows are residue. So the
wrapper no longer refuses; it selects, and ANNOUNCES the divergence on every call. That announcement
is the part under test. A wrapper that picks correctly and silently is one nobody can catch picking
wrongly later — and the reprieve is finite, because the Secret store fits at 895.4 KB against a
1,024 KB ceiling and grows ~6.7 KB per revision.
"""

from __future__ import annotations

import re
from pathlib import Path


WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "helm.sh"

#: The subcommands that WRITE the release store. Named rather than inferred from a verb list, because
#: a new mutating subcommand must be a deliberate addition here, not something a regex waves through.
_MUTATING = ("upgrade", "install", "uninstall", "delete", "rollback")


def _divergence_guard() -> str:
    body = WRAPPER.read_text()
    assert "SECRET_RELEASES=" in body, "the wrapper no longer probes the Secret store, so it cannot see a divergence"
    guard = re.search(r"if \[\[ -n \"\$SECRET_RELEASES\" \]\]; then(.+?)\nfi\n", body, re.DOTALL)
    assert guard, "the both-stores-populated guard is gone"
    return guard.group(1)


def test_a_divergence_selects_the_SECRET_store() -> None:
    """The ruling, in the one place that acts on it."""
    guard = _divergence_guard()
    assert "unset HELM_DRIVER" in guard, (
        "the wrapper still exports the SQL driver when both stores hold a release — that is the ten-day-stale upgrade the ruling exists to stop"
    )
    assert "exec helm" in guard, "the divergence branch no longer runs helm at all"


def test_the_divergence_is_ANNOUNCED_rather_than_resolved_silently() -> None:
    """Both histories are printed on every mutating call.

    A wrapper that picks correctly and silently is one nobody can catch picking wrongly: the state
    "two stores hold this release" is itself the alarm, and an operator who disagrees with the ruling
    has to be able to see it before the upgrade rather than after.
    """
    guard = _divergence_guard()
    assert "SQL/Postgres" in guard and "Secret store" in guard, "the wrapper no longer reports BOTH histories when they disagree"
    assert "owner ruling" in guard.lower(), "the selection cites no ruling, so it reads as a guess the script made"


def test_the_ceiling_that_created_the_second_store_is_recorded_with_its_measurement() -> None:
    """The Secret store fits TODAY. The header must carry the number, because the failure it replaced
    was silent — an upgrade that could not be stored at all — and a reader who does not know the margin
    cannot tell that adding to `chart/` spends it."""
    header = WRAPPER.read_text()
    assert "1,024 KB" in header, "the object-size ceiling is no longer stated"
    assert "895.4 KB" in header, "the measured headroom is gone, so the reprieve reads as a resolution"


def test_a_lone_SQL_store_still_answers_from_SQL() -> None:
    """The pre-ruling estate, and any restored from that history, must keep working: the ruling is
    about which store wins a TIE, not about deleting the SQL path."""
    body = WRAPPER.read_text()
    tail = body.split("fi\n")[-1]
    assert "HELM_DRIVER=sql" in tail, "an estate whose release lives only in Postgres can no longer reach it"
