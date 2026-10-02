"""Ed25519 signatures over a lineage event, so the author a producer stamps cannot be forged ([[LH-064]]).

A bus door authenticates the sidecar that delivered an event, not the producer that wrote it, so the author
stamped inside is a claim until something proves it. A producer signs its event with its own key and a
verifier checks the signature against that producer's published public key: recording provenance as somebody
else takes that other producer's key.

THE SIGNATURE RIDES INSIDE THE EVENT, not beside it. A header belongs to whatever carries the bytes today and
is re-agreed with every transport; a facet is part of the event and survives a Dapr retreat unchanged. A
producer signs, a verifier verifies, and neither needs to know what carried the bytes.

THE KEYS ARE ASYMMETRIC, which is what makes a second verifier safe. Each signing identity owns an Ed25519
pair: the seed is readable only by that identity's own service, and a verifier holds public keys only, so a
compromised verifier can admit or refuse events but cannot forge one. Keys travel as NATS NKEYs (an `SU...`
user seed, a `U...` public key), the text `nk` mints.

WIRE FORMAT. `rask_signature` sits on the facet bag the event already carries: `run.facets` of a RunEvent,
`dataset.facets` of a DatasetEvent. Its members are `_producer`, `_schemaURL`, `alg` ("Ed25519"), `canon` (1),
`kid`, `identity`, `onBehalfOf` (a declared delegation, optional) and `signature` (base64url, unpadded). The
signed bytes are canon-1 of the whole OpenLineage event object, the CloudEvent's data or the staged object and
never an envelope or a model dump, with exactly one member removed: `<bag>.rask_signature.signature`.

ONLY THE VALUE IS LEFT OUT, and that is the part worth stating. A signature cannot cover itself, but every
other member is a claim a hop could rewrite if it sat outside the bytes: `kid` and `canon` choose the key and the
canonicalization, so an editable one is a downgrade surface, and `onBehalfOf` is the delegation that separates an
honest service acting for a person from one stamping the wrong author.

THE SIGNER MUST BE THE AUTHOR, or declare a delegation. A signature made with the bronze-to-silver key over a
body stamped `author.sub = "service-trainer"` is valid, and the event still records provenance as somebody
else; covering the author in the bytes does not stop that. So the identity that signed has to equal the author
it stamped, or it has to declare `onBehalfOf` equal to that author and be one of the delegators the verifier is
configured with. An event with no author binds to nothing and does not verify.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import math
import re
import string
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from lineage_kit.schemas import PRODUCER


if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


#: The facet's key. IN the event rather than beside it, so a transport change never moves the signature.
SIGNATURE_FACET = "rask_signature"

#: The schema the facet's `_schemaURL` names. The URL is inside the signed bytes, so it cannot be corrected
#: after an event carries it: the git tag `facets-1.0.0` must exist before the first signed event is emitted.
SIGNATURE_FACET_SCHEMA_URL = "https://raw.githubusercontent.com/AI-Riksarkivet/rask/facets-1.0.0/spec/facets/rask/RaskSignatureRunFacet.json"

SIGNATURE_ALG = "Ed25519"

#: The canonicalization the signed bytes use, carried in the facet so a future profile is a new number.
CANON_VERSION = 1

#: Containers nested deeper than this have no canonical form. A lineage event is a handful of levels deep, and an
#: unbounded walk over a crafted one is a `RecursionError` where a refusal belongs.
MAX_NESTING = 64

_MAX_SAFE_INTEGER = 2**53 - 1

#: NKEY user seed: the seed marker `18 << 3` joined with the user type `20 << 3` spreads over two prefix bytes
#: (`SU`), then the 32-byte Ed25519 seed, then a CRC-16/XMODEM in little-endian order. Public key: the user
#: type byte (`U`), the 32-byte key, the same CRC. Base32 (RFC 4648, no padding) of that is 58 and 56 characters.
_SEED_PREFIX = bytes([0x95, 0x00])
_PUBLIC_PREFIX = bytes([0xA0])
_SEED_TEXT_LENGTH = 58
_PUBLIC_TEXT_LENGTH = 56
_NKEY_ALPHABET = re.compile(r"[A-Z2-7]+")

#: An Ed25519 signature is 64 bytes: 86 base64url characters without padding.
_SIGNATURE_TEXT = re.compile(r"[A-Za-z0-9_-]{86}")
_KID_TEXT = r"^[0-9a-f]{16}$"

#: Why a signature was not accepted, as a bounded label: a counter keyed on it stays a handful of series.
type RefusalReason = Literal[
    "unsigned",
    "malformed",
    "alg",
    "canon",
    "signer",
    "delegation",
    "author",
    "encoding",
    "kid",
    "uncanonical",
    "signature",
    "error",
]


class CanonError(ValueError):
    """The value has no canon-1 form, so it cannot be signed or checked."""


class SignatureError(Exception):
    """An event's signature cannot be accepted. `reason` is the bounded label a refusal counter is keyed on."""

    def __init__(self, message: str, reason: RefusalReason) -> None:
        super().__init__(message)
        self.reason: RefusalReason = reason


class UnsignedEventError(SignatureError):
    """The event carries no `rask_signature` facet at all."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "unsigned")


class SignatureRefusedError(SignatureError):
    """The event carries a signature that is malformed, not permitted, or does not verify."""


class KeySourceUnavailableError(RuntimeError):
    """A public-key source could not say which keys an identity publishes: an outage, never a verdict.

    A verifier that read nothing knows nothing about the signature, so the delivery is retried rather than
    refused. Refusing on an unreadable source would destroy honest events during a store blip.
    """


def canon_1(value: object) -> bytes:
    """The canon-1 bytes of a JSON-shaped value: what a signature is computed over and checked against.

    CANON-1 IS THE BINARY64 (I-JSON) PROFILE OF THIS CPYTHON CALL, and no other implementation is supported.
    A hop that decodes JSON numbers into binary64 and prints them again may turn `5.0` into `5`, so a number is
    written the way every binary64 reader reads it back: an integral float below 2^53 becomes an int, an int
    beyond 2^53-1 is refused (a binary64 hop rounds it, so the two sides would sign different bytes), and a
    non-finite number has no JSON text at all. A `bool` is never read as an int, since `True` would otherwise
    sign as `1`. Keys sort by code point, which is what `sort_keys` does.

    Raises:
        CanonError: a number, key, string or type with no canonical form, or nesting beyond `MAX_NESTING`.
    """
    normal = _normalized(value, 0)
    try:
        return json.dumps(normal, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonError("a string holds a lone surrogate, which has no UTF-8 form") from exc


def _normalized(value: object, depth: int) -> object:
    if value is None or isinstance(value, str) or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > _MAX_SAFE_INTEGER:
            raise CanonError("an integer beyond 2^53-1 has no canonical form")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise CanonError("a non-finite number has no canonical form")
        return int(value) if value.is_integer() and abs(value) < 2**53 else value
    if not isinstance(value, dict | list):
        raise CanonError(f"{type(value).__name__} has no canonical form")
    if depth >= MAX_NESTING:
        raise CanonError(f"nesting deeper than {MAX_NESTING} has no canonical form")
    if isinstance(value, list):
        return [_normalized(item, depth + 1) for item in value]
    out: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise CanonError("an object key must be a string")
        out[key] = _normalized(item, depth + 1)
    return out


def _encode_nkey(prefix: bytes, raw: bytes) -> str:
    body = prefix + raw
    return base64.b32encode(body + binascii.crc_hqx(body, 0).to_bytes(2, "little")).decode("ascii").rstrip("=")


def _decode_nkey(text: str, *, prefix: bytes, text_length: int, what: str) -> bytes:
    """The raw 32 bytes inside an NKEY of one type. `what` names the type in an error: the text is never echoed.

    STRICT, because a seed is a secret and a published key is a trust anchor. Only an uppercase RFC 4648 base32
    string of the exact length is read, only the canonical spelling of its last character (the final base32
    character carries spare bits no checksum covers), and only the one NKEY type asked for: an account or
    operator seed, or a public key where a seed belongs, is refused rather than truncated into a key.

    Raises:
        TypeError: `text` is not a string.
        ValueError: not an NKEY of the asked-for type, or its checksum does not match.
    """
    if not isinstance(text, str):
        raise TypeError(f"{what} must be a string, not {type(text).__name__}")
    text = text.strip(string.whitespace)
    if len(text) != text_length or _NKEY_ALPHABET.fullmatch(text) is None:
        raise ValueError(f"not an {what}: wrong length or alphabet")
    decoded = base64.b32decode(text + "=" * (-len(text) % 8))
    if base64.b32encode(decoded).decode("ascii").rstrip("=") != text:
        raise ValueError(f"not an {what}: not the canonical spelling")
    body, checksum = decoded[:-2], decoded[-2:]
    if binascii.crc_hqx(body, 0) != int.from_bytes(checksum, "little"):
        raise ValueError(f"not an {what}: checksum mismatch")
    if not body.startswith(prefix):
        raise ValueError(f"not an {what}: wrong key type")
    return body[len(prefix) :]


def _kid_of(raw_public: bytes) -> str:
    return hashlib.sha256(raw_public).hexdigest()[:16]


def parse_published_keys(field: str) -> list[str]:
    """The public NKEYs a `signing-public-<identity>` secret lists, current first, from its `keys` field.

    The field is comma-separated. Whitespace around an entry and empty segments are dropped, and nothing else is
    judged here: an entry that is not a key is skipped where keys are matched, so one bad entry never hides the
    others. The one parser keeps a verifier and a signer reading the same list the same way: a signer is listed,
    and so verifiable, exactly when `key.public_nkey in parse_published_keys(field)`.
    """
    return [entry for entry in (part.strip(string.whitespace) for part in field.split(",")) if entry]


class SigningKey(BaseModel):
    """An identity's Ed25519 signing key, loaded from its NKEY user seed.

    The seed is held only inside the key object: `repr` and `model_dump` carry the public NKEY and `kid` alone, so
    a key that lands in a log line or an exception shows nothing a verifier does not already publish.
    `kid` is the first 16 hex characters of sha256 of the raw public key: it names the key and is derived from it,
    so a re-minted key has a new `kid` and a `kid` is never shared by two keys.
    """

    model_config = ConfigDict(frozen=True)

    public_nkey: str
    kid: str
    _private: Ed25519PrivateKey = PrivateAttr()

    @classmethod
    def from_seed(cls, seed: str) -> SigningKey:
        """Load the key an NKEY user seed (`SU...`) encodes.

        Raises:
            TypeError: `seed` is not a string.
            ValueError: `seed` is not a well-formed NKEY user seed. The message never echoes it.
        """
        # Imported here, not at module scope: sealed runners import lineage_kit to emit over HTTP and never sign,
        # so the wheel is needed only where a key is loaded.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.from_private_bytes(_decode_nkey(seed, prefix=_SEED_PREFIX, text_length=_SEED_TEXT_LENGTH, what="NKEY user seed"))
        raw_public = private.public_key().public_bytes_raw()
        key = cls(public_nkey=_encode_nkey(_PUBLIC_PREFIX, raw_public), kid=_kid_of(raw_public))
        key._private = private
        return key

    def sign(self, message: bytes) -> bytes:
        """The 64-byte Ed25519 signature of `message`."""
        return self._private.sign(message)


class Signature(BaseModel):
    """The `rask_signature` facet an event carries, read strictly: a member of the wrong type is malformed.

    `canon` must be the int 1 (not `true`, not `1.0`) and `kid` 16 lowercase hex characters, so a facet that cannot
    name a key is refused before any key is read. Members this model does not name are ignored here and still
    covered by the signed bytes.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")

    identity: str
    kid: str = Field(pattern=_KID_TEXT)
    value: str = Field(validation_alias="signature")
    alg: str
    canon: int
    on_behalf_of: str | None = Field(default=None, validation_alias="onBehalfOf")


class VerifiedSignature(BaseModel):
    """What a verified signature established: who signed, with which key, and for whom when delegated.

    A caller that admits something on the strength of a signature (a drop only its catalog may announce) reads it
    from here rather than from the event, so it acts on what THIS verification established and not on a facet that
    is present.
    """

    model_config = ConfigDict(frozen=True)

    identity: str
    kid: str
    on_behalf_of: str | None = None


class PublicKeySource(Protocol):
    """Where a verifier reads the public NKEYs an identity publishes, current key first.

    Both reads raise `KeySourceUnavailableError` when the list cannot be read or does not exist. `refresh` reads
    again now, which a verifier asks for once after meeting a `kid` the first read did not list; it may raise
    `KeySourceUnavailableError` to decline a read it is rate-limiting, because a rate limit delays a verdict and
    never decides one.
    """

    def published(self, identity: str) -> Sequence[str]: ...

    def refresh(self, identity: str) -> Sequence[str]: ...


def _holder_key(event: Mapping[str, Any]) -> str | None:
    """Which member of the event holds its facets: `run` for a RunEvent, else `dataset` for a DatasetEvent.

    A run decides it. An event that carries both is read as a RunEvent, the way lineage parses one, so a signature
    placed on the dataset of such an event is not found and the event is unsigned.
    """
    for holder in ("run", "dataset"):
        if isinstance(event.get(holder), dict):
            return holder
    return None


def _located_bag(event: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The member holding the event's facets and the facet bag itself, or None when the event has no bag."""
    holder = _holder_key(event)
    if holder is None:
        return None
    facets = event[holder].get("facets")
    return (holder, facets) if isinstance(facets, dict) else None


def author_of(event: Mapping[str, Any]) -> str | None:
    """The subject the producer stamped on its own event, from the `author` facet's `sub`."""
    located = _located_bag(event)
    facet = located[1].get("author") if located else None
    sub = facet.get("sub") if isinstance(facet, dict) else None
    return sub if isinstance(sub, str) and sub else None


def _read_signature(event: Mapping[str, Any]) -> tuple[Signature, str]:
    """The event's signature facet and the member holding its bag. Absent is unsigned, present but wrong is malformed.

    A malformed facet is refused naming the members that are wrong and never their values, which come from whoever
    sent the event.
    """
    located = _located_bag(event)
    if located is None or SIGNATURE_FACET not in located[1]:
        raise UnsignedEventError("the event carries no signature")
    holder, bag = located
    try:
        return Signature.model_validate(bag[SIGNATURE_FACET]), holder
    except ValidationError as exc:
        wrong = sorted({".".join(str(part) for part in problem["loc"]) or "facet" for problem in exc.errors()})
        raise SignatureRefusedError(f"the signature facet is malformed at: {', '.join(wrong)}", "malformed") from None


def signature_of(event: Mapping[str, Any]) -> Signature | None:
    """The signature an event carries, or None when it carries none or carries a malformed one."""
    try:
        return _read_signature(event)[0]
    except SignatureError:
        return None


def _without_signature_value(event: Mapping[str, Any], holder: str) -> dict[str, Any]:
    """The event as it was signed: everything except the signature's own VALUE.

    A SIGNATURE CANNOT COVER ITSELF, and getting this wrong is subtle rather than loud: signing a body that
    already holds a previous signature makes every re-emit (a relay republish, an outbox drain) produce a
    different value, so the producer and the verifier disagree about an event neither has tampered with.
    Only the path to the facet is copied, never the event: the rest is shared and only read.
    """
    section = event[holder]
    facets = section["facets"]
    unsigned = {member: value for member, value in facets[SIGNATURE_FACET].items() if member != "signature"}
    return {**event, holder: {**section, "facets": {**facets, SIGNATURE_FACET: unsigned}}}


def attach_signature(payload: Mapping[str, Any], *, key: SigningKey, identity: str, on_behalf_of: str | None = None) -> dict[str, Any]:
    """Return a copy of `payload` carrying its own signature, on `run.facets` or, for a DatasetEvent, `dataset.facets`.

    `identity` NAMES THE SIGNER so a verifier knows whose published keys to read, and it is deliberately not read off
    the author facet, which is the field under attack: a verifier that keyed on `author.sub` would ask whether the key
    of whoever the event claims to be verifies it, which any producer holding its own key can satisfy for a stamp it
    has no right to.

    `on_behalf_of` DECLARES A DELEGATION, for the producer that authenticated a PERSON and emits for them, the
    catalog's case: `author.sub` is the signed-in subject and the service holds no credential of theirs. Stating it is
    what separates that from a producer stamping the wrong author. Omitted, the event is self-signed and the signer
    has to be the author, which is every service-authored run.

    THE FACET IS WRITTEN BEFORE THE VALUE IS COMPUTED, so the signer, the key id, the canonicalization and the
    delegation are all inside the signed bytes. Signing an already signed event replaces its facet, and Ed25519 is
    deterministic, so a re-emit carries the same signature.

    Raises:
        TypeError: `key` is not a `SigningKey`.
        ValueError: an empty `identity` or `on_behalf_of`, or an event with no run or dataset to carry the facet.
        CanonError: the event has no canon-1 form (a non-finite number, an integer beyond 2^53-1, nesting beyond 64).
    """
    if not isinstance(key, SigningKey):
        raise TypeError(f"a signature is made with a SigningKey, not {type(key).__name__}")
    if not identity:
        raise ValueError("a signature must name the identity that made it, or a verifier cannot choose a key")
    if on_behalf_of is not None and not on_behalf_of:
        raise ValueError("an empty on_behalf_of is a caller error: pass the subject being acted for, or omit it entirely")
    holder = _holder_key(payload)
    if holder is None:
        raise ValueError("this event carries no run or dataset, so a signature has nowhere to ride")
    facet: dict[str, Any] = {
        "_producer": PRODUCER,
        "_schemaURL": SIGNATURE_FACET_SCHEMA_URL,
        "alg": SIGNATURE_ALG,
        "canon": CANON_VERSION,
        "kid": key.kid,
        "identity": identity,
    }
    if on_behalf_of is not None:
        facet["onBehalfOf"] = on_behalf_of
    section = payload[holder]
    signed = {**payload, holder: {**section, "facets": {**(section.get("facets") or {}), SIGNATURE_FACET: facet}}}
    facet["signature"] = base64.urlsafe_b64encode(key.sign(canon_1(signed))).decode("ascii").rstrip("=")
    return copy.deepcopy(signed)


def _refused(reason: RefusalReason, message: str) -> SignatureRefusedError:
    return SignatureRefusedError(message, reason)


def _check_claim(claim: Signature, event: Mapping[str, Any], *, signers: frozenset[str], delegators: frozenset[str]) -> None:
    """The checks that need no key, in the order they are reported, so a refusal names the first thing wrong."""
    if claim.alg != SIGNATURE_ALG:
        raise _refused("alg", f"the signature names algorithm {claim.alg!r}, not {SIGNATURE_ALG}")
    if claim.canon != CANON_VERSION:
        raise _refused("canon", f"the signature names canonicalization {claim.canon}, not {CANON_VERSION}")
    if claim.identity not in signers:
        raise _refused("signer", f"{claim.identity!r} is not an identity that may sign")
    author = author_of(event)
    if claim.on_behalf_of is None:
        if claim.identity != author:
            raise _refused("author", f"{claim.identity!r} signed an event stamped for {author!r} and declared no delegation")
    elif claim.identity not in delegators:
        raise _refused("delegation", f"{claim.identity!r} is not an identity that may sign for another subject")
    elif claim.on_behalf_of != author:
        raise _refused("author", f"{claim.identity!r} declared a delegation for {claim.on_behalf_of!r} on an event stamped for {author!r}")


def _raw_signature(text: str) -> bytes:
    """The 64 signature bytes, from the one spelling an encoder writes.

    The text sits outside the signed bytes, so a decoder that took a second spelling of the same 64 bytes would let
    anyone mint a different but valid copy of an event they cannot sign, and a byte-for-byte replay check would take
    the copy for a new event. Only 86 characters of the url-safe alphabet, in their canonical form, are read.
    """
    if _SIGNATURE_TEXT.fullmatch(text) is None:
        raise _refused("encoding", "the signature is not 86 base64url characters")
    raw = base64.urlsafe_b64decode(text + "==")
    if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != text:
        raise _refused("encoding", "the signature is not the canonical base64url spelling")
    return raw


def _raw_public_key_named(listed: Iterable[str], kid: str) -> bytes | None:
    for entry in listed:
        try:
            raw = _decode_nkey(entry, prefix=_PUBLIC_PREFIX, text_length=_PUBLIC_TEXT_LENGTH, what="NKEY public key")
        except (TypeError, ValueError):
            continue
        if _kid_of(raw) == kid:
            return raw
    return None


def _published_raw_key(claim: Signature, source: PublicKeySource) -> bytes:
    """The raw public key the signature's `kid` names, from what its identity publishes.

    ONE REFRESH, THEN A VERDICT. A `kid` the first read does not list may belong to a key published after that read
    (a rotation, a re-minted signer), so the source is asked to read again once; a `kid` still unknown is refused.
    Nothing is read for an identity the caller did not list: the identity is the event's own claim and becomes part
    of a store path here.
    """
    for read in (source.published, source.refresh):
        listed = read(claim.identity)
        if not listed:
            raise KeySourceUnavailableError(f"no public keys are published for {claim.identity!r}")
        raw = _raw_public_key_named(listed, claim.kid)
        if raw is not None:
            return raw
    raise _refused("kid", f"no key {claim.identity!r} publishes has key id {claim.kid}")


@contextmanager
def _unexpected_is_a_refusal() -> Iterator[None]:
    """Turn whatever the event itself provokes into a refusal.

    An exception the verifier did not anticipate must not reach a bus handler as a failure: it would retry, and then
    park, exactly the event a forger crafted to be parked. Only what the EVENT can cause is covered; a public-key
    source failing is read outside this and is never a refusal.
    """
    try:
        yield
    except SignatureError:
        raise
    except Exception as exc:
        raise _refused("error", f"verifying the signature failed unexpectedly ({type(exc).__name__})") from exc


def _ed25519_verifies(raw_public: bytes, signature: bytes, message: bytes) -> bool:
    # Imported here for the reason `SigningKey.from_seed` states.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(raw_public).verify(signature, message)
    except InvalidSignature:
        return False
    return True


def _check_signature(event: Mapping[str, Any], holder: str, raw_public: bytes, signature: bytes) -> None:
    try:
        message = canon_1(_without_signature_value(event, holder))
    except CanonError as exc:
        raise _refused("uncanonical", f"the event has no canonical form: {exc}") from exc
    if not _ed25519_verifies(raw_public, signature, message):
        raise _refused("signature", "the signature does not verify against the published key")


def verify_signature(event: Mapping[str, Any], *, source: PublicKeySource, signers: frozenset[str], delegators: frozenset[str]) -> VerifiedSignature:
    """Verify an event's `rask_signature` against the keys its signer publishes, or raise why it cannot be accepted.

    THE CHECKS RUN IN THIS ORDER, and the order is the contract:

    1. No facet: `UnsignedEventError`. Absence is a refusal, never a pass: a verifier that treats unsigned as fine
       admits any forger who simply leaves the facet out.
    2. Without reading any key, `SignatureRefusedError`: a malformed facet, an `alg` other than Ed25519, a `canon`
       other than 1, an identity outside `signers`, a delegation from outside `delegators` or one that names a
       subject other than the stamped author, a signer that stamped another author and declared no delegation, a
       signature that is not canonical base64url.
    3. The identity's published keys are read; unreadable or empty is `KeySourceUnavailableError`, which this raises
       and never turns into a refusal. A `kid` those keys do not list asks `source.refresh` once, and a `kid` still
       unlisted is refused.
    4. The bytes are rebuilt and the Ed25519 signature checked. Anything the event provokes and nothing anticipated,
       a nesting bomb included, is a refusal too.

    `signers` and `delegators` are frozensets so that a bare string cannot stand in for one: `in` on a string is a
    substring test. Neither is empty-checked here: an empty `signers` refuses everything, and a caller that wants no
    verification does not call.

    Raises:
        UnsignedEventError: the event carries no signature.
        SignatureRefusedError: `reason` names why; a refusal is final.
        KeySourceUnavailableError: the keys could not be read; retry rather than refuse.
    """
    with _unexpected_is_a_refusal():
        claim, holder = _read_signature(event)
        _check_claim(claim, event, signers=signers, delegators=delegators)
        raw_signature = _raw_signature(claim.value)
    raw_public = _published_raw_key(claim, source)
    with _unexpected_is_a_refusal():
        _check_signature(event, holder, raw_public, raw_signature)
    return VerifiedSignature(identity=claim.identity, kid=claim.kid, on_behalf_of=claim.on_behalf_of)
