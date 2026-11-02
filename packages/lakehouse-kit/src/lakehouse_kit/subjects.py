"""Who may be addressed — the ONE rule deciding whether a value names a person.

`rask_lance.originator` is an inbox ADDRESS: `notifications`' fan-out appends it to the audience and
delivers under `NotificationReason.ORIGINATOR`. So a value that is not one person's subject is not a
weaker address — it is a row in an inbox actor named after a role, a team, or `*`, which no one reads.

A PERSON IS A PRINCIPAL NO MACHINE IDENTITY VOUCHED FOR ([[LH-063]]): `<idp-id>~<subject>`
(`service_kit.governed.principal`) with an idp id that is neither `kubernetes` nor `dex-client`. Every value that is not
a principal fails that rule without a list of its own: a role or engine literal a writer stamps as its author
(`data_eng`, `ray`), a wildcard, a userset, an FGA object id (`user:<principal>`), the anonymous fallback. A service
account's principal and a Dex client's are machines.

IT LIVES IN SERVICE-KIT BECAUSE IT HAS MORE THAN ONE PRODUCER. A value one side lets through and the
other drops is a silent miss, and one kept without sanitizing is an undeliverable address that still
acks SUCCESS, so every run-event builder and every door that accepts the claim asks this one rule.
"""

from __future__ import annotations

from service_kit.governed.principal import Principal


def is_person_subject(value: str | None) -> bool:
    """Is this value an ADDRESS for one person — the only thing `rask_lance.originator` may carry?

    One definition, used by every run-event builder and by the door that accepts the claim, because the
    two disagreeing is the whole failure mode: a value the door lets through and the builder drops is a
    silent miss, and one the builder keeps but the door never sanitized is a row in an inbox actor named
    after a role, a team, or `*`.
    """
    if not value:
        return False
    try:
        return not Principal.parse(value).is_machine
    except ValueError:
        return False
