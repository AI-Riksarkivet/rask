"""A vended credential's SCOPE is asserted by the catalog, not discovered by a client failing later.

`vending.build_session_policy` scopes every credential to one bucket + prefix, and its correctness is
the whole basis of the estate's storage story — the goal names STS as "the answer for STORAGE". But
nothing proved the object store ENFORCES the inline policy: every vending defect in this estate's
recorded history was found by a client failing afterwards (§ C1's falsy-zero guard, `vending.py`'s
hand-rolled options dict that produced a credential which was correct and unusable). A hand-run probe
settled it once on 2026-09-09; a hand-run probe is exactly what § Q17-34 objects to.

The worst case is undetectable after the fact: if the store ACCEPTS the session policy and IGNORES it,
every credential the estate ever vended reached the whole bucket and no audit record could reconstruct
it. So the check belongs at a CONFIGURATION boundary, where an operator runs it after changing the
store, the vending mode or the role — not in someone's memory.

REPORTS PER CHECK, never collapsing to the first failure: an over-permissive store and an unreachable
one are different faults with different answers, and a door that stops at the first bad news cannot
tell them apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from catalog.services.vend_probe import ProbeCheck, summarize_probe


if TYPE_CHECKING:
    from catalog.services.vend_probe import Outcome


def _c(name: str, outcome: str, detail: str = "") -> ProbeCheck:
    # `Outcome` is a Literal and these are test literals; the cast keeps the helper's signature honest
    # without spelling the union at every one of the twenty call sites below.
    return ProbeCheck(name=name, outcome=cast("Outcome", outcome), detail=detail)


def test_a_store_that_enforces_the_policy_reports_ok() -> None:
    report = summarize_probe(
        [
            _c("issue", "pass"),
            _c("write_inside", "pass"),
            _c("read_inside", "pass"),
            _c("write_outside_refused", "pass"),
            _c("cleanup", "pass"),
        ]
    )
    assert report.enforced is True
    assert report.outcome == "pass"


def test_a_store_that_IGNORES_the_policy_is_the_headline_and_says_so() -> None:
    """The one result that matters: writing outside the prefix SUCCEEDED."""
    report = summarize_probe(
        [
            _c("issue", "pass"),
            _c("write_inside", "pass"),
            _c("read_inside", "pass"),
            _c("write_outside_refused", "fail", "wrote to the parent prefix"),
            _c("cleanup", "pass"),
        ]
    )
    assert report.enforced is False, "a store that accepted a write outside the scope must not report enforced"
    assert report.outcome == "fail"


def test_every_check_is_reported_even_after_one_fails() -> None:
    """An over-permissive store and an unreachable one need telling apart, which a first-failure exit cannot do."""
    checks = [
        _c("issue", "pass"),
        _c("write_inside", "fail", "connection refused"),
        _c("read_inside", "skip", "nothing was written"),
        _c("write_outside_refused", "skip", "nothing was written"),
        _c("cleanup", "skip", "nothing was written"),
    ]
    report = summarize_probe(checks)
    assert [c.name for c in report.checks] == [c.name for c in checks], "a check was dropped from the report"
    assert report.outcome == "fail"
    # SKIP is not PASS. A scope check that never ran must never read as a scope check that succeeded.
    assert report.enforced is None, "scope was never exercised, so enforcement is UNKNOWN — not True, not False"


def test_a_vend_that_never_issued_reports_UNKNOWN_not_broken() -> None:
    """`mode_b` (server-mediated) vends nothing by design; that is a posture, not a fault."""
    report = summarize_probe(
        [
            _c("issue", "skip", "server_mediated: this warehouse vends no direct credentials"),
            _c("write_inside", "skip", "no credential"),
            _c("read_inside", "skip", "no credential"),
            _c("write_outside_refused", "skip", "no credential"),
            _c("cleanup", "skip", "no credential"),
        ]
    )
    assert report.outcome == "skip", "a warehouse that vends nothing is not a failing warehouse"
    assert report.enforced is None
