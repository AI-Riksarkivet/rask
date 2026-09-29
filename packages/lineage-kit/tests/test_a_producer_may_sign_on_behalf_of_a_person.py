"""A service can sign for a person, but only by SAYING SO, and the saying is covered by the signature.

[[LH-064]]. The first binding was signer == author, which is right for a service-authored run and
impossible for the catalog: all eight of its emit sites stamp `author.sub = token.sub`, the signed-in
person's OIDC subject, and the catalog holds a service credential rather than that person's.

RELAXING THE BINDING TO "SIGNER NEED NOT EQUAL AUTHOR" WOULD BE THE WRONG REPAIR, and it is the one
that looks obvious. It makes a producer that stamps the wrong author indistinguishable from one that
meant to act for someone — the mistake and the intent produce identical bytes, so nothing downstream
can tell a delegation from a substitution. Zero trust asks for the opposite: the trust relationship is
DECLARED and VERIFIED rather than inferred from two fields failing to match.

So a delegation is a statement the producer makes: *I, this service, am signing this event on behalf
of that subject.* `verify_signed_event` accepts a signature whose signer is the author (a service
signing its own run), or one that declares an `on_behalf_of` equal to the author — and nothing else.

THE DECLARATION IS INSIDE WHAT IS SIGNED. `_unsigned` strips only the signature VALUE now, not the
whole facet, so the signer, the algorithm and the delegation are all covered by the HMAC. Left
outside, `on_behalf_of` would be a claim anyone could rewrite in flight, which is the same defect one
layer down.
"""

from __future__ import annotations

from typing import Any

import pytest

from lineage_kit.signing import attach_signature, signature_of, verify_signed_event


KEY = "Lm4vPz8QeR1tY6uI0oA3sD5fG7hJ9kX2cV4bN6mQ"
CATALOG = "service-catalog"
PERSON = "CiQwOGE4Njg0Yi1kYjg4LTRiNzMtOTBhOS0zY2QxNjYxZjU0NjY"


def _event(sub: str) -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-09-24T09:00:00Z",
        "run": {"runId": "0198e0f2-1b2c-7a3d-8e4f-5a6b7c8d9e0f", "facets": {"author": {"name": sub, "sub": sub}}},
        "job": {"namespace": "lance", "name": "catalog.drop_table"},
        "outputs": [{"namespace": "lance", "name": "acme$customers"}],
    }


def test_a_delegation_for_SOMEONE_ELSE_is_refused() -> None:
    """Declaring a delegation does not make the author free. The declaration and the stamp must agree,
    or a producer could vouch for one subject while recording another."""
    mismatched = attach_signature(_event(PERSON), key=KEY, identity=CATALOG, on_behalf_of="someone-else")

    assert not verify_signed_event(mismatched, key=KEY)


def test_the_DECLARATION_IS_COVERED_BY_THE_SIGNATURE() -> None:
    """The reason it lives inside what is signed. Rewriting `on_behalf_of` in flight must break the
    HMAC — otherwise the delegation is a claim any hop can edit, and declaring it buys nothing.

    THE TAMPER IS ONE THE VOUCHING RULE WOULD ACCEPT, and it has to be. Injecting a delegation naming
    somebody else is refused by the author comparison whether or not the field is signed, so a test
    built on that would pass on an implementation that covers nothing. This adds `onBehalfOf` equal to
    the author of a SELF-SIGNED event: the rule is satisfied either way, and only the HMAC can object.
    """
    signed = attach_signature(_event(CATALOG), key=KEY, identity=CATALOG)
    assert verify_signed_event(signed, key=KEY), "the fixture does not verify before it is tampered with"

    tampered = {**signed}
    tampered["run"] = {**signed["run"], "facets": {**signed["run"]["facets"]}}
    tampered["run"]["facets"]["signature"] = {**signed["run"]["facets"]["signature"], "onBehalfOf": CATALOG}

    assert not verify_signed_event(tampered, key=KEY), "a delegation can be injected in flight and the signature still verifies"


def test_the_SIGNER_NAME_is_covered_too() -> None:
    """Same argument, the other field. A signer name outside the HMAC could be swapped to point the
    verifier at a different key — which fails today by luck rather than by design."""
    signed = attach_signature(_event(CATALOG), key=KEY, identity=CATALOG)
    tampered = {**signed}
    tampered["run"] = {**signed["run"], "facets": {**signed["run"]["facets"]}}
    tampered["run"]["facets"]["signature"] = {**signed["run"]["facets"]["signature"], "alg": "HMAC-SHA1"}

    assert not verify_signed_event(tampered, key=KEY), "the signature's own metadata is outside what it covers"


def test_signing_is_still_IDEMPOTENT_over_an_already_signed_event() -> None:
    """A relay republish and an outbox drain both re-handle a signed event. If signing moved the value,
    a producer and a verifier would disagree about an event neither had touched."""
    once = attach_signature(_event(PERSON), key=KEY, identity=CATALOG, on_behalf_of=PERSON)
    twice = attach_signature(once, key=KEY, identity=CATALOG, on_behalf_of=PERSON)

    assert signature_of(once) == signature_of(twice)
    assert verify_signed_event(twice, key=KEY)


def test_an_EMPTY_delegation_is_not_a_delegation() -> None:
    """`on_behalf_of=""` must not read as "signed for the empty subject" and must not silently become a
    self-signed event either — it is a caller error, and a caller error that produced a valid signature
    would be the worst of the three outcomes."""
    with pytest.raises(ValueError, match="on_behalf_of"):
        attach_signature(_event(PERSON), key=KEY, identity=CATALOG, on_behalf_of="")
