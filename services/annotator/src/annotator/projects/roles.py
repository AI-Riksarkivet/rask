"""A project's roles: owner ⊇ manager ⊇ reviewer ⊇ annotator ⊇ viewer, decided by the annotator alone (ADR 0171).

A reviewer reaches the annotator's actions because a reviewer must be able to `fix_and_accept`, which IS annotating;
publishing is the manager's act rather than a fifth role's, since it would earn nothing over the manager who froze
the project.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict


#: A grantable role on one project, most privileged first. `viewer` is not grantable: every member of the owning
#: tenant views the project, so a per-project viewer grant would change nothing while implying it did.
#: A plain alias rather than a `type` statement, because a route takes it as a query parameter.
Rung = Literal["owner", "manager", "reviewer", "annotator"]
GRANTABLE: Final[tuple[str, ...]] = get_args(Rung)
#: Every role, least privileged first: a role holds every action of the roles before it.
ORDER: Final[tuple[str, ...]] = ("viewer", "annotator", "reviewer", "manager", "owner")
#: The roles that may grant others. Losing every one leaves the project manageable only by a tenant admin.
ADMINISTRATIVE: Final[frozenset[str]] = frozenset({"owner", "manager"})

#: The least role each project action needs.
PERMISSIONS: Final[dict[str, str]] = {
    "can_view": "viewer",
    "can_send_items": "annotator",
    "can_claim": "annotator",
    "can_annotate": "annotator",
    "can_review": "reviewer",
    "can_manage": "manager",
    "can_publish": "manager",
}


class MemberGrant(BaseModel):
    """One direct grant on a project: a person and the role it gives them, as the project's actor keeps it."""

    model_config = ConfigDict(frozen=True)

    #: The person's principal key, `<idp-id>~<subject>` ([[LH-063]]).
    user: str
    relation: Rung


def holds(rung: str | None, needed: str) -> bool:
    """Whether ``rung`` reaches ``needed`` on the concentric ladder."""
    return rung is not None and ORDER.index(rung) >= ORDER.index(needed)


def highest(rungs: list[str]) -> str | None:
    """The highest of ``rungs`` on the ladder, or None when there is none."""
    known = [rung for rung in rungs if rung in ORDER]
    return max(known, key=ORDER.index) if known else None
