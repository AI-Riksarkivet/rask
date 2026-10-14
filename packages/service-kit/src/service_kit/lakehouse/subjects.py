"""Who may be addressed — the ONE rule deciding whether a value names a person.

`lance.originator` is an inbox ADDRESS: `notifications`' fan-out appends it to the audience and
delivers under `NotificationReason.ORIGINATOR`. So a value that is not one person's subject is not a
weaker address — it is a row in an inbox actor named after a role, a team, or `*`, which no one reads.

IT LIVES IN SERVICE-KIT BECAUSE IT HAS MORE THAN ONE PRODUCER. The rule was written inside the catalog
and applied there, while the medallion — the service whose authors ARE chart role literals
(`data_eng`, `analyst`, `ray`) — wrote `if originator:` and kept every value it was handed. The
medallion cannot depend on the catalog, so a shared rule had nowhere to live and the second producer
got its own interpretation by default. That is the failure the rule's own wording names: a value one
side lets through and the other drops is a silent miss, and one kept without sanitizing is an
undeliverable address that still acks SUCCESS.
"""

from __future__ import annotations

from typing import Final


#: Values that are TRUE statements about who acted and useless as an address. `data_eng`, `analyst` and
#: `ray` are role and engine literals a writer may stamp as its author and `reconcile` is the back-fill's;
#: each is true about who acted and addresses nobody, which is why the estate carries `originator` as a
#: SEPARATE field rather than overwriting attribution to fix targeting.
#:
#: NO WORKLOAD NAME BELONGS HERE. A denylist naming a modality would make this shared seam know about
#: one, which the platform is forbidden to do — so a workload whose stage authors under its own name is
#: covered by the generic checks below (or by the producer declining to claim it), never by an entry.
#: The same argument applies to an ENGINE: the durable answer is for a producer to mark its own non-person authors rather than
#: for the platform to enumerate them — this frozenset is the floor, not the design.
_NOT_A_PERSON: Final = frozenset({"", "anonymous", "anon", "system", "service", "*", "user:*", "data_eng", "analyst", "ray", "reconcile"})

#: Every subject a service-account token proves begins with this (`kubernetes~system:serviceaccount:<ns>:<sa>`,
#: ADR 0156 C5): the source `kubernetes`, then the token's `sub`. A person's IdP subject never carries it, so the
#: prefix is how a subject is known to be a machine without a list of the estate's machines.
SERVICE_ACCOUNT_SUBJECT_PREFIX: Final = "kubernetes~system:serviceaccount:"


def is_person_subject(value: str | None) -> bool:
    """Is this value an ADDRESS for one person — the only thing `lance.originator` may carry?

    One definition, used by every run-event builder and by the door that accepts the claim, because the
    two disagreeing is the whole failure mode: a value the door lets through and the builder drops is a
    silent miss, and one the builder keeps but the door never sanitized is a row in an inbox actor named
    after a role, a team, or `*`.

    Wildcards and usersets are statements about everyone, which address no one; a `user:`-prefixed value
    is an FGA object id rather than a subject; a service account's subject is a machine.
    """
    text = str(value)
    return bool(value) and value not in _NOT_A_PERSON and "#" not in text and not text.startswith(("user:", SERVICE_ACCOUNT_SUBJECT_PREFIX))
