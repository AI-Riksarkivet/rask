"""The refusals a lineage door answers with that no grant repairs: each its own type, because its verdict differs.

A plain :class:`PermissionDeniedError` names a person who lacks a grant, and writing the tuple repairs it. These four
name a defect no tuple, redelivery or restart changes, so the doors take the event and count it
(`lineage.core.metrics.refusal_outcome`) rather than park it for a repair that cannot come.
"""

from __future__ import annotations

from lance_namespace import PermissionDeniedError

from lakehouse.events.signing import RefusalReason


class UnauthoredRunError(PermissionDeniedError):
    """A bus-delivered run carrying no author at all — a defect in the MESSAGE, not a verdict on a person.

    Its own type because the verdict differs and nothing else separates the two: a named person who lacks a
    grant can be granted one, and the same event then succeeds on its next presentation, so that refusal
    is parked for repair. An author cannot be added to bytes already
    published — no tuple, no redelivery and no restart changes the answer — so re-presenting it buys
    nothing and parking it keeps a copy nothing can repair.

    Measured on the deployed estate 2026-09-18: 37 of 44 refusals in one hour were this, all for ONE run
    id, in a single burst at pod start with `already_recorded=False`, while the whole retained stream was
    replayed.

    It stops being unrepairable when the producers sign ([[LH-064]]), at which point this arm should
    stop firing rather than begin discarding more.
    """


class ForeignRunError(PermissionDeniedError):
    """An event extending a run another subject founded — refused, and no grant can change that ([[LIN-5]]).

    Its own type for the reason :class:`UnauthoredRunError` has one: the verdict differs. A named person who lacks a
    grant can be granted one, so that refusal is parked for repair; whose run this is cannot be
    granted, so re-presenting the event buys nothing, and the lane takes it and counts it unrepairable.
    """


class UngovernedOutputError(PermissionDeniedError):
    """A refusal naming outputs that carry NO authorization tuple at all — nothing to grant ON.

    Its own type for the same reason :class:`UnauthoredRunError` has one: the verdict differs and the
    message cannot carry the distinction. A named person denied on a GOVERNED table is repairable —
    write the tuple and the same event succeeds on its next presentation — so that refusal is parked
    for repair. A table FGA has no record of is a different fact: a
    grant needs an object, and re-presenting the event cannot conjure one, so parking it keeps a
    copy of an event that can never be accepted.

    Measured on the deployed estate 2026-09-19: all 7 parks in a six-hour window named four outputs —
    `e2e-ns$t74eff1b3`, `lh144b5e42f0dfns$aa`, `lh144b5e42f0dfns$bb`, `lh098bbff657cns$frag` — and
    every one carries ZERO tuples (`POST /stores/{id}/read` on `table:<id>`, 0 of 0). The first also
    answers 404 from the catalog. So the repairable arm was holding only unrepairable events.

    THE UNKNOWN CASE IS NOT THIS TYPE. A store that cannot be read is not evidence of absence, and the
    gate raises the plain :class:`PermissionDeniedError` there — parking a repairable event costs a
    kept copy, while taking an unreadable one deletes provenance, and only the second is unrecoverable.
    """


class UnverifiedEventError(PermissionDeniedError):
    """An event no listed signer's published key verifies: unsigned, forged, or signed by an identity the chart does not list ([[LH-064]]).

    Its own type for the reason :class:`UnauthoredRunError` has one: the verdict differs and the message cannot
    carry the distinction. A named person who lacks a grant is parked for repair, because a tuple can be
    written and the same event then succeeds on its next
    presentation. Bytes already published cannot gain a signature, so no grant, redelivery or restart changes
    this answer: the lane takes it and counts it (`Outcome.SIGNATURE_REFUSED`), and the drain records it and
    retires the staged object, which is the only copy a retry would have re-read.

    ``reason`` is the kit's closed :data:`~lakehouse.events.signing.RefusalReason`, the label `lineage.signature.refused`
    is keyed on; the message names what was wrong with the signature and never its bytes.

    THE OUTAGE IS NOT THIS TYPE. A public-key list that cannot be read says nothing about the signature, so
    the gate raises :class:`lance_namespace.ServiceUnavailableError` there and the lane asks for the delivery again.
    """

    def __init__(self, message: str, reason: RefusalReason) -> None:
        super().__init__(message)
        self.reason: RefusalReason = reason
