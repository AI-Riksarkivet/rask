"""How the catalog signs a control event: as itself, declaring the person it authenticated ([[XC-078]]).

The control emitter is the one signer: it signs each event before it stages and publishes it, and withholds an event it
cannot sign. The control relay signs nothing; it delivers only staged events that verify as signed by this catalog.
"""

from __future__ import annotations

from typing import Any

from lineage_kit import SigningKey, attach_control_signature
from service_kit.control_emit import ControlSign
from service_kit.governed.signing_key import SigningKeyHolder


#: How a control event names a PERSON as its actor, which is how `lineage_kit.signing` reads one too. The catalog stamps
#: every caller its door authenticated as `user:<sub>`, and no actor at all when it authenticated nobody.
_PERSON_ACTOR_PREFIX = "user:"


def control_sign(holder: SigningKeyHolder[SigningKey]) -> ControlSign:
    """The catalog's control signer over its own key holder.

    A person actor is signed for under a declared delegation (`onBehalfOf` = the whole `user:<sub>`): the catalog
    authenticated them and holds no credential of theirs, so it attests rather than impersonates. An event with no actor
    is the catalog acting for itself and declares none. The key is read from the holder on every event, so a holder that
    has lost its key raises `SigningKeyUnavailableError` instead of signing with a key its identity no longer lists.
    """

    def sign(envelope: dict[str, Any]) -> dict[str, Any]:
        actor = envelope.get("actor")
        person = actor if isinstance(actor, str) and actor.startswith(_PERSON_ACTOR_PREFIX) else None
        return attach_control_signature(envelope, key=holder.key(), identity=holder.identity, on_behalf_of=person)

    return sign
