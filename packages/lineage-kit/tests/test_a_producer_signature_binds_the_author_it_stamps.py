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
