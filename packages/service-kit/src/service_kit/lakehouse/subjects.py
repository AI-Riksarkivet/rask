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


#: Values that are TRUE statements about who acted and useless as an address. `data_eng`/`analyst` are
#: the chart's stage authors and `reconcile` is the back-fill's; the chart stamps them deliberately —
#: the stage runner really did run it — which is why the estate carries `originator` as a SEPARATE
#: field rather than overwriting attribution to fix targeting.
#:
#: NO WORKLOAD NAME BELONGS HERE. A denylist naming a modality would make this shared seam know about
#: one, which the platform is forbidden to do — so a workload whose stage authors under its own name is
#: covered by the generic checks below (or by the producer declining to claim it), never by an entry.
#: The same argument applies to an ENGINE: `ray` is listed because the chart stamps that literal as an
#: author today, and the durable answer is for a producer to mark its own non-person authors rather than
#: for the platform to enumerate them — this frozenset is the floor, not the design.
_NOT_A_PERSON: Final = frozenset({"", "anonymous", "anon", "system", "service", "*", "user:*", "data_eng", "analyst", "ray", "reconcile"})


def is_person_subject(value: str | None) -> bool:
    """Is this value an ADDRESS for one person — the only thing `lance.originator` may carry?

    One definition, used by every run-event builder and by the door that accepts the claim, because the
    two disagreeing is the whole failure mode: a value the door lets through and the builder drops is a
    silent miss, and one the builder keeps but the door never sanitized is a row in an inbox actor named
    after a role, a team, or `*`.

    Wildcards and usersets are statements about everyone, which address no one; a `user:`-prefixed value
    is an FGA object id rather than a subject.
    """
    return bool(value) and value not in _NOT_A_PERSON and "#" not in str(value) and not str(value).startswith("user:")
