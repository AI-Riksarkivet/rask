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

import hashlib
import hmac
import json
from typing import Any


#: Signed over CANONICAL JSON rather than the bytes that happened to arrive. A transport may re-encode
#: the envelope — reorder keys, change separators — and a signature valid only for one encoder's output
#: would start refusing honest producers the moment anything re-serialised it.
def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_event(payload: dict[str, Any], *, key: str) -> str:
    """HMAC-SHA256 over the whole event, hex-encoded.

    THE WHOLE EVENT, INCLUDING THE AUTHOR. Signing a subset that excludes `run.facets.author.sub`
    would verify cleanly and prevent exactly nothing, because the substitution this exists to stop
    happens inside the part left out.

    Raises `ValueError` on an empty key: an unconfigured identity must not emit a signature that every
    other holder of `""` can reproduce, which is worse than emitting none at all — it reads as proof.
    """
    if not key:
        raise ValueError("a producer signature needs a non-empty key; an unconfigured identity must emit no signature rather than a forgeable one")
    return hmac.new(key.encode("utf-8"), _canonical(payload), hashlib.sha256).hexdigest()


def verify_event(payload: dict[str, Any], signature: str, *, key: str) -> bool:
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
