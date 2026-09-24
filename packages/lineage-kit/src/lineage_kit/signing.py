"""Producer signatures over a lineage event, so a stamped author cannot be forged.

[[LH-064]]. The bus door authenticates the SIDECAR — a shared app token — and reads the author off the
payload, which `_StampedAuthor` states outright: "NOT a verified identity … nothing proves the stamp".
The output-scoped gate bounds the forgery (a forged subject must still hold the rung on every output)
but cannot stop one producer recording provenance as another.

WHY IT SITS HERE AND NOT IN A SERVICE. The row requires the seam to survive a Dapr retreat. The
envelope is the transport's; the signature is the PRODUCER's, and pinning it to a sidecar would mean
rewriting it when the transport changes. A producer signs, a verifier verifies, and neither needs to
know what carried the bytes.

THE KEY IS THE IDENTITY'S OWN CREDENTIAL, and it only became one recently enough to matter: ZT-001
made `service-token-<identity>` unguessable (`randAlphaNum 40`, not a hash of the shared app token) and
[[XC-072]] made it unreadable by peers (its own secret behind a per-app Dapr scope — measured, the
`medallion-producer` pod went from reading 8 identity credentials to 1). An HMAC keyed on the older
material would have refused an unauthenticated forger and refused none of the producer pods, which is
"non-repudiation without being it".

SYMMETRIC, AND THAT IS A DELIBERATE BOUND ON THE CLAIM. A verifier holds the same secret it verifies
with, so this proves the event came from a holder of that identity's credential — it is authentication,
not signing in the public-key sense, and it cannot prove anything to a third party who does not already
trust the verifier. That is the right strength for an in-estate bus and the wrong one for an external
attestation; anything that needs the latter wants an asymmetric key, not this.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


#: Signed over CANONICAL JSON rather than the bytes that happened to arrive. A transport may re-encode
#: the envelope — reorder keys, change separators — and a signature valid only for one encoder's output
#: would start refusing honest producers the moment anything re-serialised it.
def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_event(payload: Mapping[str, Any], *, key: str) -> str:
    """HMAC-SHA256 over the whole event, hex-encoded.

    THE WHOLE EVENT, INCLUDING THE AUTHOR. Signing a subset that excludes `run.facets.author.sub`
    would verify cleanly and prevent exactly nothing, because the substitution this exists to stop
    happens inside the part left out.

    Raises `ValueError` on an empty key: an unconfigured identity must not emit a signature that every
    other holder of `""` can reproduce, which is worse than emitting none at all — it reads as proof.
    """
    if not key:
        raise ValueError("a producer signature needs a non-empty key; an unconfigured identity must emit no signature rather than a forgeable one")
    return hmac.new(key.encode("utf-8"), _canonical(_unsigned(payload)), hashlib.sha256).hexdigest()


def verify_event(payload: Mapping[str, Any], signature: str, *, key: str) -> bool:
    """True when `signature` was made over this exact event with this key.

    ANSWERS FALSE ON JUNK, never raises. This runs on the ingest path, so a verifier that threw on a
    malformed header would turn a forged request into a 500 and hand any unauthenticated caller a way
    to fail the door — the refusal has to be a refusal.

    `compare_digest` rather than `==`: both values are attacker-influenced here, and a short-circuiting
    comparison leaks the matching prefix length one request at a time.

    NO SEPARATE EMPTY-INPUT GUARD, deliberately. An earlier draft opened with
    ``if not key or not signature: return False``; mutation-checking showed removing it changed no
    outcome, because an empty key already raises out of :func:`sign_event` into the handler below and an
    empty signature already fails the compare. A branch no test can reach is not a defence, it is an
    untested claim about one.
    """
    try:
        expected = sign_event(payload, key=key)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


#: The run facet the signature rides in. IN the event rather than beside it, because the row requires a
#: signature that survives a Dapr retreat: a header belongs to whatever is carrying the bytes today, and
#: would have to be re-agreed when that changes. A self-carrying event needs no side channel.
SIGNATURE_FACET = "signature"

#: rask's own facet, so it is namespaced like `author` and `lance` rather than claiming a spec slot.
_PRODUCER = "https://github.com/AI-Riksarkivet/rask/tree/main/packages/lineage-kit/src/lineage_kit/signing.py"


@dataclass(frozen=True)
class Signature:
    """A signature read off an event, with the identity whose key is supposed to verify it."""

    identity: str
    value: str


def _facets(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The facet bag a signature lives in — a run's for a RunEvent, the dataset's for a DatasetEvent."""
    for holder in ("run", "dataset"):
        section = payload.get(holder)
        if isinstance(section, dict):
            facets = section.get("facets")
            if isinstance(facets, dict):
                return facets
    return None


def _unsigned(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The event as it was BEFORE a signature was attached.

    A SIGNATURE CANNOT COVER ITSELF, and getting this wrong is subtle rather than loud: signing a body
    that already holds a previous signature makes every re-emit — a relay republish, an outbox drain —
    produce a different value, so the producer and the verifier disagree about an event neither has
    tampered with. Stripping first makes signing idempotent.
    """
    # `dict(...)` before the deepcopy, because the input is a read-only Mapping: the door hands over
    # the arrived CloudEvent as it stands rather than copying it first, and the copy belongs here where
    # it is about to be mutated.
    stripped: dict[str, Any] = copy.deepcopy(dict(payload))
    facets = _facets(stripped)
    if facets is not None:
        facets.pop(SIGNATURE_FACET, None)
    return stripped


def signature_of(payload: Mapping[str, Any]) -> Signature | None:
    """The signature an event carries, or None when it carries none."""
    facets = _facets(payload) or {}
    facet = facets.get(SIGNATURE_FACET)
    if not isinstance(facet, dict):
        return None
    identity, value = facet.get("identity"), facet.get("signature")
    if isinstance(identity, str) and isinstance(value, str) and identity and value:
        return Signature(identity=identity, value=value)
    return None


def attach_signature(payload: Mapping[str, Any], *, key: str, identity: str) -> dict[str, Any]:
    """Return a copy of `payload` carrying its own signature.

    `identity` NAMES THE SIGNER so the verifier knows which key to try, and it is deliberately NOT read
    off the author facet — that is the field under attack. A verifier that keyed on `author.sub` would
    ask "does the key belonging to whoever this claims to be verify it?", which any producer holding
    its own key can satisfy for a stamp it has no right to. Keying on the signature's own `identity`
    and then requiring it to match the author is what makes the substitution fail.
    """
    if not identity:
        raise ValueError("a signature must name the identity that made it, or a verifier cannot choose a key")
    signed = _unsigned(payload)
    facets = _facets(signed)
    if facets is None:
        raise ValueError("this event carries no run or dataset facet bag, so a signature has nowhere to ride")
    facets[SIGNATURE_FACET] = {
        "_producer": _PRODUCER,
        "alg": "HMAC-SHA256",
        "identity": identity,
        "signature": sign_event(signed, key=key),
    }
    return signed


def author_of(payload: Mapping[str, Any]) -> str | None:
    """The subject the producer stamped on its own event, from the `author` facet."""
    facets = _facets(payload) or {}
    facet = facets.get("author")
    sub = facet.get("sub") if isinstance(facet, dict) else None
    return sub if isinstance(sub, str) and sub else None


def verify_signed_event(payload: Mapping[str, Any], *, key: str) -> bool:
    """True when the event carries a signature that `key` reproduces AND the signer is the author.

    An event carrying NO signature answers False. Absence is a refusal, never a pass — a door that
    treats "unsigned" as "fine" is the door this replaces.

    THE BINDING IS THE POINT, AND COVERING THE AUTHOR IS NOT ENOUGH ON ITS OWN. A signature made with
    the bronze-to-silver key, naming bronze-to-silver as its signer, over a body stamped
    ``author.sub = "service-trainer"``, is a VALID signature: the verifier picks the key the signature
    names, reproduces it exactly, and agrees — while the event records provenance as somebody else.
    Every individual check passes and the substitution survives. So the identity that SIGNED must equal
    the author it stamped; without that, this proves only "some authorised producer emitted it", which
    the shared app token already proved.

    An event with no author does not verify either. Admitting it would make the binding optional, and a
    forger would simply omit the facet.
    """
    found = signature_of(payload)
    if found is None:
        return False
    author = author_of(payload)
    if author is None or author != found.identity:
        return False
    return verify_event(_unsigned(payload), found.value, key=key)
