"""Prove a vended credential is SCOPED, at a configuration boundary rather than in someone's memory.

`core/vending.py::build_session_policy` scopes every credential to one bucket + prefix, and the goal
names STS as "the answer for STORAGE" — so the whole storage posture rests on the object store
ENFORCING the inline session policy it is handed. Nothing asserted that. Every vending defect this
estate has recorded was found by a client failing later: § C1's falsy-zero guard, and the hand-rolled
options dict at `vending.py` that produced a credential which was correct and unusable.

THE WORST CASE IS UNDETECTABLE AFTER THE FACT. A store that ACCEPTS the policy and IGNORES it hands
out bucket-wide credentials while every log line reads normal; no audit record could reconstruct which
of them over-reached. That is a question to ask when the store, the vending mode or the role changes —
which is what makes it a door rather than a runbook step.

THIS MODULE IS THE PURE HALF: the check vocabulary and how a set of results reduces to one verdict.
The IO half lives at the endpoint, so the reduction is testable without an object store, and so the
rule that `skip` is not `pass` is stated once rather than at each call site.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field


Outcome = Literal["pass", "fail", "skip"]

#: The check whose result IS the security claim. The others describe reachability; only this one
#: answers "does the store honour the scope", so only this one may set `enforced`.
SCOPE_CHECK: Final = "write_outside_refused"

#: The check whose result IS the COMMIT-SAFETY claim, and the only one that may set `commit_safe`.
#: `lance_docs/file_format.md` § "Commit Protocol" -> "Storage Primitives": Lance commits rely on
#: put-if-not-exists, and those primitives are what "guarantee that exactly one writer succeeds when
#: multiple writers attempt to create the same manifest file concurrently". A store that ACCEPTS a
#: second conditional put has no such guarantee, and the loss is silent — both writers see success.
CAS_CHECK: Final = "conditional_put_refused"

#: Its setup step: the FIRST conditional put, which must be accepted. Separate because a store that
#: rejects the header outright and one that ignores it are different operator problems — the first
#: proves nothing about a second writer (UNKNOWN), the second is a live hazard (UNSAFE).
CAS_RESERVE: Final = "conditional_put"


class ProbeCheck(BaseModel):
    """One step of the probe, with its own outcome — never folded into a neighbour's."""

    name: str
    outcome: Outcome
    detail: str = ""


class ProbeReport(BaseModel):
    """What the door answers. `enforced` is deliberately three-valued.

    `None` means the scope check never ran — a credential was not issued, or the write that would have
    proven it failed for an unrelated reason. Reporting that as `False` would raise a false alarm about
    the store, and reporting it as `True` would claim a guarantee nothing tested. An unexercised control
    is UNKNOWN, which is the same rule `index_health` applies to an index whose stats will not read.
    """

    outcome: Outcome
    enforced: bool | None = None
    #: Whether the store honours put-if-not-exists, the primitive Lance's commit protocol rests on.
    #: THREE-VALUED for the same reason `enforced` is: `None` means the question was never asked — no
    #: credential, or the store refused the conditional header itself, which proves nothing about what
    #: a second concurrent writer would see. Reporting that as `False` raises a false alarm about the
    #: store; reporting it as `True` claims a guarantee nothing tested.
    commit_safe: bool | None = None
    checks: list[ProbeCheck] = Field(default_factory=list)


def summarize_probe(checks: list[ProbeCheck]) -> ProbeReport:
    """Reduce every check to one verdict WITHOUT dropping any of them.

    A first-failure exit cannot tell an over-permissive store from an unreachable one, and those need
    different answers — so the caller runs every step it can and this reduces the whole list.
    """
    by_name = {c.name: c for c in checks}
    scope = by_name.get(SCOPE_CHECK)
    enforced = None if scope is None or scope.outcome == "skip" else scope.outcome == "pass"
    cas = by_name.get(CAS_CHECK)
    commit_safe = None if cas is None or cas.outcome == "skip" else cas.outcome == "pass"

    if any(c.outcome == "fail" for c in checks):
        outcome: Outcome = "fail"
    elif all(c.outcome == "skip" for c in checks):
        outcome = "skip"
    else:
        outcome = "pass"
    return ProbeReport(outcome=outcome, enforced=enforced, commit_safe=commit_safe, checks=list(checks))
