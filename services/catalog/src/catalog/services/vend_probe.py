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
The calls on the store are `storage_probe`'s, so the reduction is testable without an object store, and the
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
#: `lance_docs/lance/format/table/transaction.md` § "Commit Protocol" -> "Storage Primitives": Lance commits rely on
#: put-if-not-exists, and those primitives are what "guarantee that exactly one writer succeeds when
#: multiple writers attempt to create the same manifest file concurrently". A store that ACCEPTS a
#: second conditional put has no such guarantee, and the loss is silent — both writers see success.
CAS_CHECK: Final = "conditional_put_refused"

#: Its setup step: the FIRST conditional put, which must be accepted. Separate because a store that
#: rejects the header outright and one that ignores it are different operator problems — the first
#: proves nothing about a second writer (UNKNOWN), the second is a live hazard (UNSAFE).
CAS_RESERVE: Final = "conditional_put"

#: Writes and reads back an object encrypted as the vend tells its writer to (``vending.encryption``), run only when the
#: vend names an encryption. It follows a plain write the store accepted, so its failure is the store refusing that
#: encryption, its role holding no grant on the KMS key, and not a store it could not reach ([[LH-262]]).
ENCRYPTED_WRITE: Final = "encrypted_write"


#: The checks whose results ARE the managed-versioning claim ([[LH-511]], ADR 0157), each asked of a WRITE-tier
#: credential. The store refuses it a final manifest, at a key the tier's own staged-commit Allow also matches, so only
#: an honoured explicit Deny refuses it: a store that evaluates Allows alone lets any writer finalize a version past
#: ``version/create``.
FINAL_MANIFEST_CHECK: Final = "final_manifest_refused"
#: And it refuses a tag or branch pointer under ``_refs/``, which no statement of the tier allows: a store that reduces
#: a session policy to its prefix lets a writer pin a version an erasure removes. The scope check cannot show this,
#: since the probe tier it runs under may put anywhere in its prefix.
REF_CHECK: Final = "ref_put_refused"

#: Their setup: the write-tier credential is issued, and stages a manifest the tier allows. Either failing means the
#: two refusals above were never asked of a credential that works, which says nothing about the store's Deny.
WRITE_TIER_ISSUE: Final = "issue_write_tier"
STAGED_MANIFEST: Final = "staged_manifest"

#: The check a step leaves beside its own when the store did not answer its request. The step measured nothing, so its
#: own check is ``skip``, and this one fails, naming the step in its detail. It describes reachability, as the setup
#: steps do: a report holding it says the probe did not finish, never that the store broke a guarantee.
STORE_ANSWERED: Final = "store_answered"


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
    #: Whether the store refuses a writer's credential the final manifest and every ref, which managed versioning
    #: rests on. THREE-VALUED as the two above: ``None`` means neither refusal was asked of a write credential that works.
    versions_guarded: bool | None = None
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
    guards = [check for name in (FINAL_MANIFEST_CHECK, REF_CHECK) if (check := by_name.get(name)) is not None and check.outcome != "skip"]
    versions_guarded = all(check.outcome == "pass" for check in guards) if guards else None

    if any(c.outcome == "fail" for c in checks):
        outcome: Outcome = "fail"
    elif all(c.outcome == "skip" for c in checks):
        outcome = "skip"
    else:
        outcome = "pass"
    return ProbeReport(outcome=outcome, enforced=enforced, commit_safe=commit_safe, versions_guarded=versions_guarded, checks=list(checks))
