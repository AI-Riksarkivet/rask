"""A producer's Ed25519 signature binds the event it signs, author included, to a key its identity publishes.

[[LH-064]] C3. The lineage bus door authenticates the sidecar, not the producer, so the author stamped on an event
is a claim. The signature turns it into one a listed signer's published key vouches for, and `lineage_kit.signing`
is the one implementation every signer and verifier imports.

[[XC-078]]. A control event is the same claim in another layout: a flat object whose `actor` plays the author and whose
facet is a top-level member. The `control-` rows drive `verify_control_signature` through the same outcome table, so what
differs is only whom a signer may vouch for: a delegator for a `user:` actor, any listed signer for a service.

WHAT PINS THE WIRE FORMAT IS NOT THIS PACKAGE. The root conftest's `EventSigner` writes the contract's NKEY keys,
canon-1 and `rask_signature` facet from the contract alone, so the signatures here are compared with an
independent writer's rather than checked against the code that made them, and an RFC 8032 vector anchors the key
text and the Ed25519 itself. The doors that call the verifier (lineage's four) have their own tests; this file
pins the function: what it accepts, the order in which it refuses, and when it reads a key at all.

THE TAMPER ROWS ARE CHOSEN SO ONLY THE SIGNATURE CAN OBJECT, wherever the structural checks would not already
catch it. An identity swapped to another delegator, a delegation injected into a catalog event, and a person
retargeted in the author and the delegation together all pass every check that needs no key, so each is refused
only if the member is inside the signed bytes. Leaving the whole facet out of the signed bytes, instead of only its
signature value, turns those rows green: that is the mutation they exist to catch.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from lineage_kit import (
    CanonError,
    KeySourceUnavailableError,
    SignatureError,
    SigningKey,
    VerifiedSignature,
    attach_control_signature,
    attach_signature,
    canon_1,
    control_signature_of,
    parse_published_keys,
    signature_of,
    verify_control_signature,
    verify_signature,
)
from lineage_kit.signing import MAX_NESTING


STAGE, CATALOG, OTHER_DELEGATOR, ROGUE = "service-bronze-to-silver", "service-catalog", "service-other-delegator", "service-trainer"
PERSON = "CgVhbGljZRIFbG9jYWw"
PERSON_ACTOR = f"user:{PERSON}"
SIGNERS = frozenset({STAGE, CATALOG, OTHER_DELEGATOR})
DELEGATORS = frozenset({CATALOG, OTHER_DELEGATOR})

_REPO = Path(__file__).resolve().parents[3]
_FACET_SCHEMA = _REPO / "spec" / "facets" / "rask" / "RaskSignatureRunFacet.json"
_OPENLINEAGE_SCHEMA = _REPO / "tests" / "data" / "openlineage-2-0-2.json"

_BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _event(author: str = STAGE) -> dict[str, Any]:
    """A medallion COMPLETE: its `lance` facet carries a float that happens to be integral."""
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "producer": "https://example.invalid/producer",
        "run": {
            "runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
            "facets": {"author": {"sub": author, "name": author}, "lance": {"duration_seconds": 5.0, "rows": 12}},
        },
        "job": {"namespace": "lance", "name": "stage.silver"},
        "outputs": [{"namespace": "silver", "name": "acme-silver$events"}],
    }


def _dataset_event(author: str) -> dict[str, Any]:
    return {
        "eventTime": "2026-10-02T12:00:00+00:00",
        "dataset": {"namespace": "lance", "name": "acme-bronze$dropped", "facets": {"lance": {"operation": "drop_table"}, "author": {"sub": author}}},
    }


def _control_event(actor: str | None = PERSON_ACTOR) -> dict[str, Any]:
    """A `grant_added` control event as the bus carries it (`CatalogControlEvent.model_dump_json()` read back): a flat object with no facet bag."""
    return {
        "event_id": "0199a1b2c3d47e5f8a9b0c1d2e3f4a5b",
        "occurred_at": "2026-10-04T12:00:00.123456Z",
        "action": "grant_added",
        "object_type": "grant",
        "object_id": "table:acme-silver$events",
        "actor": actor,
        "extra": {"relation": "can_read", "subject": "user:bob"},
    }


def _with_signature_members(signed: dict[str, Any], **members: Any) -> dict[str, Any]:
    """The event with members of its `rask_signature` facet replaced and nothing else touched."""
    bag = signed["run"]["facets"]
    return {**signed, "run": {**signed["run"], "facets": {**bag, "rask_signature": {**bag["rask_signature"], **members}}}}


def _with_control_signature_members(signed: dict[str, Any], **members: Any) -> dict[str, Any]:
    """The control event with members of its top-level `rask_signature` replaced and nothing else touched."""
    return {**signed, "rask_signature": {**signed["rask_signature"], **members}}


def _with_author(signed: dict[str, Any], sub: str) -> dict[str, Any]:
    bag = signed["run"]["facets"]
    return {**signed, "run": {**signed["run"], "facets": {**bag, "author": {**bag["author"], "sub": sub}}}}


def _nested(containers: int) -> list[Any]:
    value: list[Any] = []
    for _ in range(containers - 1):
        value = [value]
    return value


class _Source:
    """What each identity publishes; a re-read finds the same lists (the door tests drive rotation and outages)."""

    def __init__(self, published: Mapping[str, Sequence[str]]) -> None:
        self._published = published
        self.calls: list[str] = []

    def published(self, identity: str) -> Sequence[str]:
        self.calls.append("published")
        return self._published.get(identity, [])

    def refresh(self, identity: str) -> Sequence[str]:
        self.calls.append("refresh")
        return self._published.get(identity, [])


def _signed(signer: Any, event: dict[str, Any], *, on_behalf_of: str | None = None) -> dict[str, Any]:
    """The event signed by the code under test with an independent writer's key.

    The conformance test anchors that code to the independent writer once. The verifier rows sign with it, so a
    mutation of how the bytes are built moves the signer and the verifier together, which is the failure an
    independent signer would hide: every row would fail alike and the tamper rows would still read as refused.
    """
    return attach_signature(event, key=SigningKey.from_seed(signer.seed), identity=signer.identity, on_behalf_of=on_behalf_of)


def _signed_control(signer: Any, envelope: dict[str, Any], *, on_behalf_of: str | None = None) -> dict[str, Any]:
    """`_signed` for a control event, by the same argument."""
    return attach_control_signature(envelope, key=SigningKey.from_seed(signer.seed), identity=signer.identity, on_behalf_of=on_behalf_of)


def _facet_schema_errors(facets: dict[str, Any]) -> list[str]:
    openlineage = json.loads(_OPENLINEAGE_SCHEMA.read_text())
    registry: Registry[Any] = Registry().with_resource(openlineage["$id"], Resource.from_contents(openlineage, default_specification=DRAFT202012))
    validator = Draft202012Validator(json.loads(_FACET_SCHEMA.read_text()), registry=registry, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(facets)]


def test_a_signature_is_the_wire_format_the_contract_writes_and_verifies_against_the_published_key(event_signer: Any) -> None:
    # RFC 8032 section 7.1 test 1 as an NKEY user seed: the key text, the kid and plain Ed25519 against an outside reference.
    rfc = SigningKey.from_seed("SUAJ2YNRTXX72WTAXKCEV5ES5QWMIRCJYVUXWMTJDFYDXLADDSXH6YALCA")
    assert (rfc.public_nkey, rfc.kid) == ("UDLVVGABQKYQVN6VJP7NHSLEA45A5YLS6PNKMIZFV4BBU2HXA5IRUVAL", "21fe31dfa154a261")
    assert rfc.sign(b"").hex() == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )

    catalog = event_signer(CATALOG)
    key = SigningKey.from_seed(f" {catalog.seed}\n")  # a seed read from a store carries whatever whitespace its writer left
    assert (key.public_nkey, key.kid) == (catalog.public, catalog.kid)
    assert catalog.seed not in f"{key!r} {key.model_dump()}", "a key shown in a log line or an exception carries its seed"

    # The independent writer produces the same event byte for byte, on a RunEvent's run facets and a DatasetEvent's dataset facets.
    run = attach_signature(_event(PERSON), key=key, identity=CATALOG, on_behalf_of=PERSON)
    dataset = attach_signature(_dataset_event(PERSON), key=key, identity=CATALOG, on_behalf_of=PERSON)
    assert run == catalog.sign(_event(PERSON), on_behalf_of=PERSON)
    assert dataset == catalog.sign(_dataset_event(PERSON), on_behalf_of=PERSON)
    assert _facet_schema_errors(run["run"]["facets"]) == [], "the facet is not what spec/facets/rask/RaskSignatureRunFacet.json describes"

    expected = VerifiedSignature(identity=CATALOG, kid=catalog.kid, on_behalf_of=PERSON)
    source = _Source({CATALOG: [catalog.public]})
    assert verify_signature(run, source=source, signers=SIGNERS, delegators=DELEGATORS) == expected
    assert verify_signature(dataset, source=source, signers=SIGNERS, delegators=DELEGATORS) == expected

    # Signing a signed event again changes nothing: a relay republish and an outbox drain both re-handle a signed event, and
    # Ed25519 is deterministic.
    original = _event(PERSON)
    again = attach_signature(original, key=key, identity=CATALOG, on_behalf_of=PERSON)
    assert attach_signature(again, key=key, identity=CATALOG, on_behalf_of=PERSON) == again
    found = signature_of(again)
    assert found is not None
    assert (found.identity, found.kid, found.on_behalf_of) == (CATALOG, catalog.kid, PERSON)
    assert signature_of(_event()) is None
    assert parse_published_keys(f" {catalog.public} ,, {rfc.public_nkey}\n") == [catalog.public, rfc.public_nkey]

    # The signed event is a copy: changing it leaves the event it was made from alone.
    again["run"]["facets"]["lance"]["rows"] = 99
    assert original == _event(PERSON)

    # A control event has no facet bag: the same facet rides as a top-level member, and the independent writer produces it byte for byte.
    control = attach_control_signature(_control_event(), key=key, identity=CATALOG, on_behalf_of=PERSON_ACTOR)
    assert control == catalog.sign_control(_control_event(), on_behalf_of=PERSON_ACTOR)
    assert _facet_schema_errors({"rask_signature": control["rask_signature"]}) == [], "the control facet is not what the facet schema describes"
    delegated = VerifiedSignature(identity=CATALOG, kid=catalog.kid, on_behalf_of=PERSON_ACTOR)
    assert verify_control_signature(control, source=source, signers=SIGNERS, delegators=DELEGATORS) == delegated
    assert attach_control_signature(control, key=key, identity=CATALOG, on_behalf_of=PERSON_ACTOR) == control, "a re-signed control event changed"
    claimed = control_signature_of(control)
    assert claimed is not None
    assert (claimed.identity, claimed.kid, claimed.on_behalf_of) == (CATALOG, catalog.kid, PERSON_ACTOR)
    assert control_signature_of(_control_event()) is None
    # What could never verify is refused where the producer still sees it: a person's event with no delegation, and a signer with no name.
    with pytest.raises(ValueError, match="declared delegation"):
        attach_control_signature(_control_event(), key=key, identity=CATALOG)
    with pytest.raises(ValueError, match="name the identity"):
        attach_control_signature(_control_event(None), key=key, identity="")


@pytest.mark.parametrize(
    ("case", "outcome", "reads"),
    [
        pytest.param("corrupt-entry-beside-the-key", "verified", ["published"], id="a-corrupt-published-entry-does-not-hide-the-right-one"),
        pytest.param("nothing-published", "unavailable", ["published"], id="an-identity-with-no-published-key"),
        pytest.param("malformed-canon", "malformed", [], id="a-facet-whose-canon-is-a-boolean"),
        pytest.param("malformed-kid", "malformed", [], id="a-facet-whose-kid-is-not-sixteen-hex-characters"),
        pytest.param("signer-stamps-another-author", "author", [], id="a-signer-stamping-another-author-without-a-delegation"),
        pytest.param("delegation-names-another-subject", "author", [], id="a-delegation-for-someone-other-than-the-stamped-author"),
        pytest.param("signature-of-the-wrong-length", "encoding", [], id="a-signature-that-is-not-86-base64url-characters"),
        pytest.param("second-spelling-of-the-signature", "encoding", [], id="a-second-spelling-of-the-same-signature-bytes"),
        pytest.param("nested-600-deep", "uncanonical", ["published"], id="an-event-nested-far-beyond-the-bound"),
        pytest.param("tamper-alg", "alg", [], id="the-algorithm-rewritten"),
        pytest.param("tamper-canon", "canon", [], id="the-canonicalization-rewritten"),
        pytest.param("tamper-schema-url", "signature", ["published"], id="the-schema-url-rewritten"),
        pytest.param("tamper-author", "signature", ["published"], id="the-person-retargeted-in-the-author-and-the-delegation"),
        pytest.param("a-fault-in-the-crypto-library", "error", ["published"], id="an-exception-nothing-anticipated"),
        pytest.param("control-person-by-a-non-delegator", "delegation", [], id="a-control-event-for-a-person-signed-by-a-signer-that-is-no-delegator"),
        pytest.param("control-person-without-a-delegation", "author", [], id="a-control-event-for-a-person-signed-with-no-delegation-declared"),
        pytest.param("control-delegation-for-another-actor", "author", [], id="a-control-delegation-for-someone-other-than-the-actor"),
        pytest.param("control-signer-outside-both-sets", "signer", [], id="a-control-event-signed-by-an-identity-nothing-lists"),
        pytest.param("control-tamper-facet", "signature", ["published"], id="a-control-signature-whose-schema-url-was-rewritten"),
    ],
)
def test_the_verifier_accepts_what_a_published_key_signed_and_refuses_the_rest_reading_a_key_only_when_it_must(
    monkeypatch: pytest.MonkeyPatch, event_signer: Any, case: str, outcome: str, reads: list[str]
) -> None:
    catalog, stage = event_signer(CATALOG), event_signer(STAGE)
    published = {CATALOG: [catalog.public], STAGE: [stage.public]}
    delegated = _signed(catalog, _event(PERSON), on_behalf_of=PERSON)
    self_signed = _signed(stage, _event(STAGE))
    control_delegated = _signed_control(catalog, _control_event(), on_behalf_of=PERSON_ACTOR)
    corrupt = stage.public[:10] + ("A" if stage.public[10] != "A" else "B") + stage.public[11:]
    # The last base64url character of a signature carries bits no one checks, so it has a second spelling for the same 64 bytes.
    value = self_signed["run"]["facets"]["rask_signature"]["signature"]
    second_spelling = value[:-1] + _BASE64URL[_BASE64URL.index(value[-1]) ^ 1]

    def nested_600_deep() -> tuple[dict[str, Any], _Source]:
        bomb = {**self_signed, "run": {**self_signed["run"], "facets": {**self_signed["run"]["facets"], "lance": {"deep": _nested(600)}}}}
        return bomb, _Source(published)

    def a_fault_in_the_crypto_library() -> tuple[dict[str, Any], _Source]:
        def fail(_raw: bytes) -> None:
            raise RuntimeError("the library failed in a way nothing anticipated")

        monkeypatch.setattr(ed25519.Ed25519PublicKey, "from_public_bytes", fail)
        return self_signed, _Source(published)

    scenarios: dict[str, Callable[[], tuple[dict[str, Any], _Source]]] = {
        "corrupt-entry-beside-the-key": lambda: (self_signed, _Source({STAGE: [corrupt, stage.public]})),
        "nothing-published": lambda: (self_signed, _Source({CATALOG: [catalog.public]})),
        "malformed-canon": lambda: (_with_signature_members(self_signed, canon=True), _Source(published)),
        "malformed-kid": lambda: (_with_signature_members(self_signed, kid="NOT-A-KID"), _Source(published)),
        "signer-stamps-another-author": lambda: (_signed(stage, _event(ROGUE)), _Source(published)),
        "delegation-names-another-subject": lambda: (_signed(catalog, _event(PERSON), on_behalf_of="someone-else"), _Source(published)),
        "signature-of-the-wrong-length": lambda: (_with_signature_members(self_signed, signature=value[:-1]), _Source(published)),
        "second-spelling-of-the-signature": lambda: (_with_signature_members(self_signed, signature=second_spelling), _Source(published)),
        "nested-600-deep": nested_600_deep,
        "tamper-alg": lambda: (_with_signature_members(self_signed, alg="Ed25519ph"), _Source(published)),
        "tamper-canon": lambda: (_with_signature_members(self_signed, canon=2), _Source(published)),
        "tamper-schema-url": lambda: (_with_signature_members(self_signed, _schemaURL="https://example.invalid/other.json"), _Source(published)),
        "tamper-author": lambda: (
            _with_signature_members(_with_author(delegated, "someone-else"), onBehalfOf="someone-else"),
            _Source(published),
        ),
        "a-fault-in-the-crypto-library": a_fault_in_the_crypto_library,
        "control-person-by-a-non-delegator": lambda: (_signed_control(stage, _control_event(), on_behalf_of=PERSON_ACTOR), _Source(published)),
        # The kit refuses to sign this, so the independent writer does: a verifier must not rely on every signer being the kit.
        "control-person-without-a-delegation": lambda: (stage.sign_control(_control_event()), _Source(published)),
        "control-delegation-for-another-actor": lambda: (_signed_control(catalog, _control_event(), on_behalf_of="user:someone-else"), _Source(published)),
        "control-signer-outside-both-sets": lambda: (_signed_control(event_signer(ROGUE), _control_event(None)), _Source(published)),
        "control-tamper-facet": lambda: (
            _with_control_signature_members(control_delegated, _schemaURL="https://example.invalid/other.json"),
            _Source(published),
        ),
    }
    event, source = scenarios[case]()
    verify = verify_control_signature if case.startswith("control-") else verify_signature

    try:
        verify(event, source=source, signers=SIGNERS, delegators=DELEGATORS)
        decided = "verified"
    except SignatureError as exc:
        decided = exc.reason
    except KeySourceUnavailableError:
        decided = "unavailable"

    assert decided == outcome, f"{case}: decided {decided}"
    assert source.calls == reads, f"{case}: read {source.calls}"


@pytest.mark.parametrize(
    ("case", "refused"),
    [
        pytest.param("bad-checksum", ValueError, id="a-seed-with-a-character-changed"),
        pytest.param("an-account-seed", ValueError, id="a-well-formed-seed-of-another-nkey-type"),
        pytest.param("lowercase", ValueError, id="a-seed-in-lower-case"),
        pytest.param("second-spelling", ValueError, id="a-second-spelling-of-the-same-seed"),
        pytest.param("bytes", TypeError, id="a-seed-that-is-not-text"),
    ],
)
def test_a_seed_that_is_not_an_nkey_user_seed_is_refused_without_echoing_it(event_signer: Any, case: str, refused: type[Exception]) -> None:
    seed = event_signer(STAGE).seed
    account_seed = bytes([0x90, 0x00]) + bytes(32)  # the seed marker with the account type, a well-formed NKEY of the wrong kind
    account_seed_text = base64.b32encode(account_seed + binascii.crc_hqx(account_seed, 0).to_bytes(2, "little")).decode().rstrip("=")
    bad: str | bytes = {
        "bad-checksum": seed[:20] + ("A" if seed[20] != "A" else "B") + seed[21:],
        "an-account-seed": account_seed_text,
        "lowercase": seed.lower(),
        # The last base32 character of a seed carries two bits no checksum covers, so a second spelling decodes to the same key.
        "second-spelling": seed[:-1] + _BASE32[_BASE32.index(seed[-1]) ^ 1],
        "bytes": seed.encode(),
    }[case]

    with pytest.raises(refused, match="NKEY user seed") as raised:
        SigningKey.from_seed(cast("str", bad))  # the bytes row hands over what the annotation forbids, on purpose

    shown = str(raised.value)
    assert seed not in shown and (bad if isinstance(bad, str) else seed) not in shown, "an error message carries the seed it refused"


def test_canon_1_writes_the_binary64_profile_of_canonical_json() -> None:
    value = {
        "z": [True, None, "é<&>\u2028"],
        "a": {"integral": 5.0, "negative_zero": -0.0, "largest_safe": 2**53 - 1, "float_2_60": 2.0**60, "1e16": 1e16, "1e22": 1e22, "fraction": 0.1},
        "b": 12,
    }
    assert (
        canon_1(value)
        == (
            '{"a":{"1e16":1e+16,"1e22":1e+22,"float_2_60":1.152921504606847e+18,"fraction":0.1,"integral":5,"largest_safe":9007199254740991,"negative_zero":0},'
            '"b":12,"z":[true,null,"é<&>\u2028"]}'
        ).encode()
    )
    assert canon_1(_nested(MAX_NESTING)) == b"[" * MAX_NESTING + b"]" * MAX_NESTING, "the deepest permitted nesting is refused"


@pytest.mark.parametrize(
    ("value", "why"),
    [
        pytest.param({1: "x"}, "key", id="a-key-that-is-not-a-string"),
        pytest.param("\ud800", "surrogate", id="a-lone-surrogate"),
        pytest.param((1, 2), "tuple", id="a-type-json-has-no-name-for"),
    ],
)
def test_canon_1_refuses_what_has_no_canonical_form(value: object, why: str) -> None:
    with pytest.raises(CanonError, match=why):
        canon_1(value)
