"""A bus producer's signature must make its stamped author unforgeable.

[[LH-064]]. The lineage bus door authenticates the SIDECAR — a shared app token — and then reads the
author off the payload. `_StampedAuthor` says so plainly: "NOT a verified identity … nothing proves the
stamp". The gate bounds the forgery (a forged subject must still hold the rung on every output) but it
cannot stop one producer recording provenance as another.

WHY THIS BECAME POSSIBLE ONLY NOW. A signature needs a key that DISTINGUISHES a producer, and until
2026-09-23 no such key existed in a usable form: ZT-001 made `service-token-<identity>` unguessable
(`randAlphaNum 40` rather than `sha256("<identity>-<dapr.appToken>")[:40]`), and [[XC-072]] made it
unreadable by peers (its own secret, per-app Dapr scope; measured 8 credentials -> 1 for
`medallion-producer`). Before both, an HMAC keyed on this material would have refused an
unauthenticated forger and refused NONE of the producer pods — "non-repudiation without being it", in
the row's own words.

THE SIGNATURE COVERS THE AUTHOR, which is the whole point and the one property a naive implementation
drops. Signing an event body that excludes `author.sub` would verify perfectly while leaving exactly
the substitution this exists to prevent.

IT LIVES IN `lineage-kit`, NOT IN A SERVICE, because the row requires it to survive a Dapr retreat: the
transport is what carries the envelope, and a seam bolted to the sidecar would have to be rewritten
when that changes.
"""

from __future__ import annotations

from typing import Any

import pytest

from lineage_kit.signing import sign_event, verify_event


KEY = "AhaS3VaDMfKnpj82CKQTvmIGP200Nl85NKvug7Wm"
OTHER = "hBvaR8ijWCnpj82CKQTvmIGP200Nl85NKvug7WmF"


def _event(sub: str = "service-bronze-to-silver") -> dict[str, Any]:
    return {
        "eventTime": "2026-09-23T19:00:00Z",
        "producer": "https://example.invalid/producer",
        "run": {"runId": "0198e0f2-1b2c-7a3d-8e4f-5a6b7c8d9e0f", "facets": {"author": {"sub": sub, "name": sub}}},
        "job": {"namespace": "lance", "name": "stage.silver"},
        "outputs": [{"namespace": "lance", "name": "silver$features"}],
    }


def test_a_signature_verifies_with_the_key_that_made_it() -> None:
    assert verify_event(_event(), sign_event(_event(), key=KEY), key=KEY)


def test_ANOTHER_PRODUCERS_KEY_DOES_NOT_VERIFY() -> None:
    """The property XC-072 bought: a peer cannot mint this signature because it cannot read the key."""
    assert not verify_event(_event(), sign_event(_event(), key=KEY), key=OTHER)


def test_SUBSTITUTING_THE_AUTHOR_BREAKS_THE_SIGNATURE() -> None:
    """The defect this row exists to close, stated as a test.

    IF THIS IS RED the signature covers a body that excludes the stamp, which verifies cleanly and
    prevents nothing — a producer could still record provenance as any subject it liked.
    """
    signed = sign_event(_event(sub="service-bronze-to-silver"), key=KEY)
    forged = _event(sub="service-trainer")
    assert not verify_event(forged, signed, key=KEY), "the author was swapped and the signature still verified"


def test_KEY_ORDER_DOES_NOT_CHANGE_THE_SIGNATURE() -> None:
    """A transport may re-serialise the envelope, so the input has to be canonical, not textual.

    Without this, a signature is valid only for the exact byte order one JSON encoder happened to emit
    and every honest producer starts failing the moment anything re-encodes.
    """
    a = dict(_event())
    b = {k: a[k] for k in reversed(list(a))}
    assert sign_event(a, key=KEY) == sign_event(b, key=KEY)


def test_A_MALFORMED_SIGNATURE_IS_FALSE_NOT_AN_EXCEPTION() -> None:
    """The door must answer 403, not 500. A verifier that raises on junk is a DoS on the ingest path."""
    for junk in ("", "not-hex", "ab", "z" * 64):
        assert verify_event(_event(), junk, key=KEY) is False


def test_AN_EMPTY_KEY_IS_REFUSED_RATHER_THAN_SIGNING_WITH_NOTHING() -> None:
    """An unconfigured identity must not produce a signature every other holder of "" can reproduce."""
    with pytest.raises(ValueError, match="key"):
        sign_event(_event(), key="")


# --------------------------------------------------------------------------- #
# Carrying the signature IN the event, which is what makes it transport-independent.
# --------------------------------------------------------------------------- #


def test_a_SIGNED_event_verifies_itself_without_a_side_channel() -> None:
    """The row asks for a signature over the CloudEvent that survives a Dapr retreat.

    A header would tie it to whatever is carrying the bytes today, so the signature rides IN the event
    and the event verifies itself. That forces the one subtlety below: a signature cannot cover itself.
    """
    from lineage_kit.signing import attach_signature, verify_signed_event

    signed = attach_signature(_event(), key=KEY, identity="service-bronze-to-silver")
    assert verify_signed_event(signed, key=KEY)


def test_SIGNING_IS_IDEMPOTENT_because_the_signature_cannot_cover_itself() -> None:
    """Re-signing an already-signed event must produce the same signature.

    IF THIS IS RED the signature is being computed over a body that includes a previous signature, so
    every re-emit (a relay republish, an outbox drain) changes it and no verifier can agree with the
    producer.
    """
    from lineage_kit.signing import attach_signature, signature_of

    once = attach_signature(_event(), key=KEY, identity="service-bronze-to-silver")
    twice = attach_signature(once, key=KEY, identity="service-bronze-to-silver")
    assert signature_of(once) == signature_of(twice)


def test_AN_UNSIGNED_EVENT_DOES_NOT_VERIFY() -> None:
    """Absence of a signature is a refusal, never a pass. A door that treats "no signature" as "fine"
    is the door we already have."""
    from lineage_kit.signing import verify_signed_event

    assert not verify_signed_event(_event(), key=KEY)


def test_THE_SIGNATURE_FACET_NAMES_THE_IDENTITY_THE_VERIFIER_MUST_KEY_ON() -> None:
    """The verifier holds many keys and must know which to try, and it must NOT take that from the
    author facet — that is the field under attack. The signature names its own signer, and a mismatch
    between the two is the forgery this closes."""
    from lineage_kit.signing import attach_signature, signature_of

    signed = attach_signature(_event(), key=KEY, identity="service-bronze-to-silver")
    found = signature_of(signed)
    assert found is not None and found.identity == "service-bronze-to-silver"


def test_TAMPERING_WITH_THE_AUTHOR_OF_A_SIGNED_EVENT_IS_CAUGHT() -> None:
    """The end-to-end form of the property, through the carrying API rather than the primitive."""
    from lineage_kit.signing import attach_signature, verify_signed_event

    signed = attach_signature(_event(sub="service-bronze-to-silver"), key=KEY, identity="service-bronze-to-silver")
    signed["run"]["facets"]["author"]["sub"] = "service-trainer"
    assert not verify_signed_event(signed, key=KEY)


def test_A_PRODUCER_CANNOT_SIGN_FOR_AN_AUTHOR_THAT_IS_NOT_ITSELF() -> None:
    """THE FORGERY THE WHOLE ROW IS ABOUT, and covering the author is NOT enough to stop it.

    A signature over a body containing `author.sub = "service-trainer"`, made with the
    bronze-to-silver key and naming bronze-to-silver as its signer, is a perfectly valid signature. The
    verifier looks up the key for the identity the signature names, reproduces it, and agrees — while
    the event records provenance as somebody else. Every check passes and the substitution survives.

    So the seam has to bind the two: the identity that SIGNED must be the author it stamped. Without
    this the signature proves an event was emitted by some authorised producer, which is what the
    shared app token already proved.
    """
    from lineage_kit.signing import attach_signature, verify_signed_event

    forged = attach_signature(_event(sub="service-trainer"), key=KEY, identity="service-bronze-to-silver")
    assert not verify_signed_event(forged, key=KEY), (
        "a producer signed its own key over ANOTHER subject's stamp and the event verified — the signature proves only that some authorised producer emitted it"
    )


def test_SIGNING_FOR_YOURSELF_STILL_VERIFIES() -> None:
    """The control for the test above: the binding must refuse the forgery and admit the honest case."""
    from lineage_kit.signing import attach_signature, verify_signed_event

    honest = attach_signature(_event(sub="service-bronze-to-silver"), key=KEY, identity="service-bronze-to-silver")
    assert verify_signed_event(honest, key=KEY)


def test_AN_EVENT_WITH_NO_AUTHOR_AT_ALL_DOES_NOT_VERIFY() -> None:
    """An unstamped event has nothing to bind to, and admitting it would make the binding optional —
    a forger would simply omit the author facet."""
    from lineage_kit.signing import attach_signature, verify_signed_event

    bare: dict[str, Any] = {"eventTime": "2026-09-23T19:00:00Z", "run": {"runId": "r", "facets": {}}, "outputs": []}
    assert not verify_signed_event(attach_signature(bare, key=KEY, identity="service-bronze-to-silver"), key=KEY)


def test_THE_PRIMITIVE_ITSELF_IGNORES_AN_ALREADY_ATTACHED_SIGNATURE() -> None:
    """`sign_event` drops the signature VALUE before hashing, so it is safe for a caller that did not.

    Both callers inside this module drop it first, which made the drop invisible to every test —
    mutation-checking found removing it changed no outcome. That is untested safety, and the fix is to
    exercise it rather than delete it: a verifier or a relay handed a signed event must get the same
    digest the producer computed, not one over a body containing the old value.

    The digest over a BARE event is a different number, and deliberately so: the facet's own claims —
    who signed, with which algorithm, on whose behalf — are inside what the HMAC covers, because a
    claim left outside it can be rewritten by any hop. So the equality that matters is
    `sign_event(signed) == found.value`, and a body that carries no facet at all is simply a different
    document.
    """
    from lineage_kit.signing import attach_signature, sign_event, signature_of

    unsigned = _event()
    signed = attach_signature(unsigned, key=KEY, identity="service-bronze-to-silver")
    found = signature_of(signed)
    assert found is not None
    assert sign_event(signed, key=KEY) == found.value
    assert sign_event(unsigned, key=KEY) != found.value, "the signature's own claims are outside what it covers"
