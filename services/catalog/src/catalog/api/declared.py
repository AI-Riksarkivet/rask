"""Who makes a control-plane change, and the lock git holds on what it declares ([[XC-213]], ADR 0206).

A project or a warehouse is made and changed by a signed-in caller at a door, or by the catalog itself converging what
git declares (``catalog.api.declaration_converge``). Both go through the same functions, which name whoever made the
change as its :class:`Author`. A record git declares carries ``managed_by: git``, and a door asked to change what git
declares of it refuses: the declaration is the only writer of those fields, so the estate is what git says.
"""

from typing import Final

from lance_namespace import PermissionDeniedError
from pydantic import BaseModel, ConfigDict

from catalog.api.security import ANONYMOUS_SUBJECT, Caller, actor_of
from catalog.services.control_records import GIT
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit


class Author(BaseModel):
    """Who a control-plane change is made by."""

    model_config = ConfigDict(frozen=True)

    #: The writer recorded on a tuple it writes and on a record it creates.
    subject: str
    #: The actor its control events and records name: a caller's OpenFGA user (`catalog.api.security.actor_of`).
    actor: str


#: The catalog converging what git declares: no caller at a door.
DECLARATION: Final = Author(subject="system:declaration", actor="system:declaration")


def author_of(token: Caller | None) -> Author | None:
    """The author a signed-in caller is, or ``None`` on a stack that authenticates nobody."""
    return None if token is None else Author(subject=token.subject, actor=actor_of(token))


def refuse_what_git_declares(manager: str | None, *, obj: str, token: Caller | None) -> None:
    """Refuse a door's change to ``obj`` while git declares it, audited as a denial.

    Asked after the door's own authorization, so only a caller who could otherwise make the change learns that git
    owns it. Grants, namespaces and tables beneath the object stay with their doors.

    Raises:
        PermissionDeniedError: ``manager``, who the object's record says owns its declaration, is git.
    """
    if manager != GIT:
        return
    subject = token.subject if token is not None else ANONYMOUS_SUBJECT
    audit(AuditAction.AUTHZ, AuditOutcome.DENY, subject=subject, resource=obj, reason="managed_by_git")
    raise PermissionDeniedError(
        f"{obj} is declared in git, which owns its status, deletion protection, delete profile and existence. Change the declaration (the "
        "chart's `catalog.declared` values) and the catalog converges it; a door cannot."
    )
