"""The signing vectors of ``gen_port_vectors.py``: what the Python verifier says about a signed, tampered or unsigned event.

Every event is signed with the real ``lakehouse.lineage.signing.attach_signature`` (or its control twin) and judged by
the real ``verify_signature`` and ``verify_control_signature``, then by lineage's own ``verify_arrived_event`` and
``_attested``. Each event is recorded as the JSON text that arrives, so a reader sees the bytes Python read, and with the
canon-1 bytes of the document as it was signed. The key pairs are derived from a fixture seed here, so the vectors are
the same on every run and the repository holds no private key.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import random
import struct
from collections.abc import Callable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any


#: The seed every test key pair is derived from: a fixture's, protecting nothing.
FIXTURE_SEED = "rask-port-signing-vectors-fixture-seed"
_T = "2026-11-17T10:00:00Z"
_PERSON = "dex~ada"
_DELEGATOR = "catalog"
_SERVICE = "svc-ingest"


def _key(name: str) -> Any:  # noqa: ANN401 - the kit's SigningKey, imported lazily as the generator does
    from lakehouse.lineage.signing import _SEED_PREFIX, SigningKey, _encode_nkey

    raw = hashlib.sha256(f"{FIXTURE_SEED}:{name}".encode()).digest()
    return SigningKey.from_seed(_encode_nkey(_SEED_PREFIX, raw))


def _run(author: str | None, **extra: Any) -> dict[str, Any]:  # noqa: ANN401 - JSON-shaped members
    facets: dict[str, Any] = {}
    if author is not None:
        facets["rask_author"] = {"_producer": "https://example.test/p", "sub": author}
    return {
        "eventType": "COMPLETE",
        "eventTime": _T,
        "producer": "https://example.test/p",
        "run": {"runId": "0190a1b2-0000-7000-8000-000000000001", "facets": facets},
        "job": {"namespace": "wh", "name": "pages"},
        "inputs": [],
        "outputs": [{"namespace": "lance", "name": "wh$raw$pages"}],
        **extra,
    }


def _dataset(author: str | None) -> dict[str, Any]:
    facets: dict[str, Any] = {"rask_lance": {"tableUuid": "0190a1b2-0000-7000-8000-00000000000a"}}
    if author is not None:
        facets["rask_author"] = {"sub": author}
    return {"eventTime": _T, "producer": "https://example.test/p", "dataset": {"namespace": "lance", "name": "wh$raw$pages", "facets": facets}}


def _control(actor: str | None) -> dict[str, Any]:
    event: dict[str, Any] = {"action": "grant_added", "object": "table:wh$raw$pages", "occurred_at": _T, "project": "wh"}
    if actor is not None:
        event["actor"] = actor
    return event


def _facet(event: dict[str, Any]) -> dict[str, Any]:
    holder = "run" if isinstance(event.get("run"), dict) else "dataset"
    return event[holder]["facets"]["rask_signature"]


def _with(event: dict[str, Any], change: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    changed = copy.deepcopy(event)
    change(changed)
    return changed


def _reversed(value: object) -> object:
    """``value`` with every object's members in reverse order: the same document, written another way."""
    if isinstance(value, dict):
        return {key: _reversed(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reversed(item) for item in value]
    return value


def _nested(depth: int) -> object:
    value: object = "bottom"
    for _ in range(depth):
        value = [value]
    return value


def _event_cases() -> list[dict[str, Any]]:
    from lakehouse.lineage.signing import attach_control_signature, attach_signature

    service, catalog, outsider, old = _key(_SERVICE), _key(_DELEGATOR), _key("outsider"), _key("svc-ingest-old")

    def signed(event: dict[str, Any], key: Any = service, identity: str = _SERVICE, on_behalf_of: str | None = None) -> dict[str, Any]:  # noqa: ANN401
        return attach_signature(event, key=key, identity=identity, on_behalf_of=on_behalf_of)

    def control(event: dict[str, Any], key: Any = catalog, identity: str = _DELEGATOR, on_behalf_of: str | None = None) -> dict[str, Any]:  # noqa: ANN401
        return attach_control_signature(event, key=key, identity=identity, on_behalf_of=on_behalf_of)

    def sig(value: object) -> Callable[[dict[str, Any]], object]:
        return lambda event: _facet(event).__setitem__("signature", value)

    def member(name: str, value: object) -> Callable[[dict[str, Any]], object]:
        return lambda event: _facet(event).__setitem__(name, value)

    own = signed(_run(_SERVICE))
    delegated = signed(_run(_PERSON), key=catalog, identity=_DELEGATOR, on_behalf_of=_PERSON)
    good_value = _facet(own)["signature"]
    flipped = ("B" if good_value[10] == "A" else "A").join([good_value[:10], good_value[11:]])
    rich = _run(
        _SERVICE,
        custom={
            "text": 'Ström, 東京, emoji 😀, quote " backslash \\ newline \n tab \t nul \u0000 del \u007f sep \u2028',
            "floats": [0.1, -2.5, 1e-07, 1.5e300, 12345678901234567.0, 9007199254740992.0, 5.0, -0.0, 1e16],
            "ints": [0, -1, 9007199254740991, -9007199254740991],
            "nested": [[1, [2, [3, None]]], [], {}, [None, True, False]],
            "nulls": None,
            "ключ": {"z": 1, "a": 2, "É": 3, "e": 4, "😀": 5, "\uffff": 6},
        },
    )
    rich_signed = signed(rich)

    cases: list[tuple[str, str, object]] = [
        ("a run event its author signed", "event", own),
        ("a dataset event its author signed", "event", signed(_dataset(_SERVICE))),
        ("a run event a delegator signed for a person", "event", delegated),
        ("a dataset event a delegator signed for a person", "event", signed(_dataset(_PERSON), key=catalog, identity=_DELEGATOR, on_behalf_of=_PERSON)),
        ("a run event signed with an older published key", "event", signed(_run(_SERVICE), key=old)),
        ("non-ascii strings, floats, extreme integers, nested arrays and nulls", "event", rich_signed),
        ("a changed field", "event", _with(own, lambda event: event.__setitem__("eventType", "FAIL"))),
        ("a changed author", "event", _with(own, lambda event: event["run"]["facets"]["rask_author"].__setitem__("sub", "svc-ingest "))),
        ("a changed member of the facet", "event", _with(own, member("_producer", "https://example.test/other"))),
        ("an added member", "event", _with(own, lambda event: event.__setitem__("extra", 1))),
        ("a reordered object is the same document", "event", _reversed(own)),
        ("a reordered array", "event", _with(rich_signed, lambda event: event["custom"]["nested"].reverse())),
        ("a removed author facet", "event", _with(own, lambda event: event["run"]["facets"].pop("rask_author"))),
        ("a removed signature facet", "event", _with(own, lambda event: event["run"]["facets"].pop("rask_signature"))),
        ("a removed signature value", "event", _with(own, lambda event: _facet(event).pop("signature"))),
        ("an unknown key id", "event", _with(own, member("kid", "0000000000000000"))),
        ("another identity's key id", "event", _with(own, member("kid", catalog.kid))),
        ("a signer that is not accepted", "event", signed(_run("outsider"), key=outsider, identity="outsider")),
        ("a signer naming an accepted identity with its own key", "event", signed(_run(_SERVICE), key=outsider)),
        ("a signer stamping another author with no delegation", "event", signed(_run(_PERSON))),
        ("a delegator stamping a person with no delegation", "event", signed(_run(_PERSON), key=catalog, identity=_DELEGATOR)),
        ("a delegator declaring another subject", "event", signed(_run(_PERSON), key=catalog, identity=_DELEGATOR, on_behalf_of="dex~bob")),
        ("a delegation from a signer that is no delegator", "event", signed(_run(_PERSON), on_behalf_of=_PERSON)),
        ("a self-delegation from a signer that is no delegator", "event", signed(_run(_SERVICE), on_behalf_of=_SERVICE)),
        ("a delegator signing for itself under a delegation", "event", signed(_run(_DELEGATOR), key=catalog, identity=_DELEGATOR, on_behalf_of=_DELEGATOR)),
        ("a delegator signing its own event unaided", "event", signed(_run(_DELEGATOR), key=catalog, identity=_DELEGATOR)),
        ("a delegation on an event with no author", "event", signed(_run(None), key=catalog, identity=_DELEGATOR, on_behalf_of=_PERSON)),
        ("a signed event with no author", "event", signed(_run(None))),
        ("an author that is not a string", "event", signed(_run(_SERVICE) | {"run": {"runId": "r", "facets": {"rask_author": {"sub": 7}}}})),
        ("an author facet that is a string", "event", signed(_run(_SERVICE) | {"run": {"runId": "r", "facets": {"rask_author": _SERVICE}}})),
        ("an empty author", "event", signed(_run(""), key=catalog, identity=_DELEGATOR, on_behalf_of=_PERSON)),
        ("an empty delegation", "event", _with(delegated, member("onBehalfOf", ""))),
        ("a null delegation", "event", _with(signed(_run(_SERVICE)), member("onBehalfOf", None))),
        ("a facet that is a string", "event", _with(own, lambda event: event["run"]["facets"].__setitem__("rask_signature", "signed"))),
        ("a facet that is a list", "event", _with(own, lambda event: event["run"]["facets"].__setitem__("rask_signature", []))),
        ("a facet with no key id", "event", _with(own, lambda event: _facet(event).pop("kid"))),
        ("a facet with no identity", "event", _with(own, lambda event: _facet(event).pop("identity"))),
        ("a facet with no algorithm", "event", _with(own, lambda event: _facet(event).pop("alg"))),
        ("a facet with no canon", "event", _with(own, lambda event: _facet(event).pop("canon"))),
        ("a key id in capitals", "event", _with(own, member("kid", _facet(own)["kid"].upper()))),
        ("a key id with a trailing newline", "event", _with(own, member("kid", _facet(own)["kid"] + "\n"))),
        ("a key id that is short", "event", _with(own, member("kid", _facet(own)["kid"][:15]))),
        ("a key id that is a number", "event", _with(own, member("kid", 1234567890123456))),
        ("an identity that is a number", "event", _with(own, member("identity", 7))),
        ("an algorithm that is a list", "event", _with(own, member("alg", ["Ed25519"]))),
        ("a delegation that is a number", "event", _with(own, member("onBehalfOf", 7))),
        ("a signature that is a number", "event", _with(own, sig(7))),
        ("canon as true", "event", _with(own, member("canon", True))),
        ("canon as a float", "event", _with(own, member("canon", 1.0))),
        ("canon as text", "event", _with(own, member("canon", "1"))),
        ("canon 2", "event", _with(own, member("canon", 2))),
        ("canon beyond 64 bits", "event", _with(own, member("canon", 10**30))),
        ("an algorithm in lower case", "event", _with(own, member("alg", "ed25519"))),
        ("an unknown algorithm before an unknown signer", "event", _with(own, lambda event: _facet(event).update(alg="RS256", identity="nobody"))),
        ("an unknown signer before a bad encoding", "event", _with(own, lambda event: _facet(event).update(identity="nobody", signature="x"))),
        ("a signature that is short", "event", _with(own, sig(good_value[:85]))),
        ("a signature with padding", "event", _with(own, sig(good_value + "=="))),
        ("a signature in the standard alphabet", "event", _with(own, sig(good_value[:84] + "+/"))),
        ("a signature with spare bits set", "event", _with(own, sig(good_value[:85] + ("B" if good_value[85] == "A" else "F")))),
        ("a signature with non-ascii characters", "event", _with(own, sig(good_value[:85] + "é"))),
        ("a signature that does not verify", "event", _with(own, sig(flipped))),
        ("an integer beyond 2^53-1", "event", _with(own, lambda event: event.__setitem__("big", 9007199254740992))),
        ("a negative integer beyond 2^53-1", "event", _with(own, lambda event: event.__setitem__("big", -(10**40)))),
        ("nesting deeper than canon-1", "event", _with(own, lambda event: event.__setitem__("deep", _nested(70)))),
        ("nesting at the canon-1 bound", "event", signed(_run(_SERVICE, deep=_nested(61)))),
        ("an event with no facets", "event", _run(_SERVICE) | {"run": {"runId": "r"}}),
        ("an event whose facets are a list", "event", _run(_SERVICE) | {"run": {"runId": "r", "facets": []}}),
        ("an event with neither run nor dataset", "event", {"eventTime": _T, "job": {"namespace": "wh", "name": "pages"}}),
        ("a run that is not an object", "event", signed(_dataset(_SERVICE) | {"run": "r"})),
        ("a signature on the dataset of a run event", "event", signed(_dataset(_SERVICE)) | {"run": {"runId": "r", "facets": {}}}),
        ("an event that is a list", "event", [own]),
        ("an event that is a string", "event", "rask_signature"),
        ("an event that is null", "event", None),
        ("a control event a service signed", "control", control(_control("system:maintenance"))),
        ("a control event with no actor", "control", control(_control(None))),
        ("a control event a delegator signed for a person", "control", control(_control("user:dex~ada"), on_behalf_of="user:dex~ada")),
        (
            "a control event for a person delegated by a signer that is no delegator",
            "control",
            control(_control("user:dex~ada"), key=service, identity=_SERVICE, on_behalf_of="user:dex~ada"),
        ),
        ("a control event for a person delegated for the bare principal", "control", control(_control("user:dex~ada"), on_behalf_of="dex~ada")),
        ("a control event for a service under a delegation", "control", control(_control("system:maintenance"), on_behalf_of="user:dex~ada")),
        ("a control event with a changed action", "control", _with(control(_control(None)), lambda event: event.__setitem__("action", "grant_revoked"))),
        (
            "a control event with a changed key id",
            "control",
            _with(control(_control(None)), lambda event: event["rask_signature"].__setitem__("kid", "0" * 16)),
        ),
        ("a control event that carries no signature", "control", _control(None)),
        ("a control event whose facet is null", "control", _control(None) | {"rask_signature": None}),
        ("a control event an outsider signed", "control", control(_control(None), key=outsider, identity="outsider")),
    ]
    # A float its producer wrote as `5.0` verifies whether a hop writes it `5.0` or `5`, and exotic number text reads the same.
    texts: list[tuple[str, str, str]] = [
        ("a whole float written as an int", "event", json.dumps(signed(_run(_SERVICE, n=5.0))).replace('"n": 5.0', '"n": 5')),
        ("a whole float written with an exponent", "event", json.dumps(signed(_run(_SERVICE, n=500.0))).replace('"n": 500.0', '"n": 5E+2')),
        ("a float written with extra digits", "event", json.dumps(signed(_run(_SERVICE, n=0.1))).replace('"n": 0.1', '"n": 0.1000000000000000000001')),
        ("a NaN literal", "event", json.dumps(signed(_run(_SERVICE))).replace('"inputs": []', '"inputs": [NaN]')),
        ("an Infinity literal", "event", json.dumps(signed(_run(_SERVICE))).replace('"inputs": []', '"inputs": [-Infinity]')),
        ("a float beyond binary64", "event", json.dumps(signed(_run(_SERVICE))).replace('"inputs": []', '"inputs": [1e400]')),
        ("a lone surrogate", "event", json.dumps(signed(_run(_SERVICE))).replace('"inputs": []', '"inputs": ["\\ud800"]')),
        ("a lone surrogate as a key", "event", json.dumps(signed(_run(_SERVICE))).replace('"inputs": []', '"inputs": [{"\\udc00": 1}]')),
        ("an escaped surrogate pair", "event", json.dumps(signed(_run(_SERVICE, n="😀")), ensure_ascii=True)),
        ("a member written twice keeps the last", "event", json.dumps(own).replace('"eventType": "COMPLETE"', '"eventType": "FAIL", "eventType": "COMPLETE"')),
        ("a facet written twice keeps the last", "event", json.dumps(own).replace('"rask_signature": {', '"rask_signature": {"kid": 1}, "rask_signature": {')),
        ("whitespace around the document", "event", " \n\t" + json.dumps(own, indent=2) + "\r\n"),
        ("escaped solidus and short escapes", "event", json.dumps(signed(_run(_SERVICE, n="a/b\b\f"))).replace("a/b", "a\\/b")),
    ]
    return [{"name": name, "kind": kind, "arrived": json.dumps(event, ensure_ascii=False)} for name, kind, event in cases] + [
        {"name": name, "kind": kind, "arrived": text} for name, kind, text in texts
    ]


def _published() -> dict[str, list[str]]:
    """What each identity publishes, current key first. Entries that are not keys are skipped, never fatal."""
    service, catalog, outsider, old = _key(_SERVICE), _key(_DELEGATOR), _key("outsider"), _key("svc-ingest-old")
    return {
        _SERVICE: [service.public_nkey, "not-a-key", old.public_nkey.lower(), old.public_nkey],
        _DELEGATOR: [catalog.public_nkey],
        "outsider": [outsider.public_nkey],
    }


class _Listed:
    """A key source over recorded lists: a refresh reads the same list, and an identity not held publishes nothing."""

    def __init__(self, published: Mapping[str, Sequence[str]]) -> None:
        self._published = published

    def published(self, identity: str) -> Sequence[str]:
        return self._published.get(identity, [])

    def refresh(self, identity: str) -> Sequence[str]:
        return self.published(identity)


def _verdict(verify: Callable[[], Any]) -> dict[str, Any]:
    from lakehouse.lineage.signing import KeySourceUnavailableError, SignatureError

    try:
        verified = verify()
    except SignatureError as exc:
        return {"refused": exc.reason}
    except KeySourceUnavailableError:
        return {"unavailable": True}
    return {"verified": {"identity": verified.identity, "kid": verified.kid, "on_behalf_of": verified.on_behalf_of}}


def _signed_bytes(kind: str, arrived: object) -> str | None:
    """canon-1 of the document as it was signed, or None when it carries no facet or has no canonical form."""
    from lakehouse.lineage.signing import CanonError, _control_without_signature_value, _holder_key, _located_bag, _without_signature_value, canon_1

    if not isinstance(arrived, dict):
        return None
    try:
        if kind == "control":
            document = _control_without_signature_value(arrived) if isinstance(arrived.get("rask_signature"), dict) else None
        else:
            located = _located_bag(arrived)
            holder = _holder_key(arrived)
            facet = located[1].get("rask_signature") if located else None
            document = _without_signature_value(arrived, holder) if holder and isinstance(facet, dict) else None
        return canon_1(document).decode("utf-8") if document is not None else None
    except CanonError:
        return None


def _key_sets() -> dict[str, dict[str, list[str]]]:
    published = _published()
    return {
        "published": published,
        "service unpublished": {name: keys for name, keys in published.items() if name != _SERVICE},
        "service publishes only junk": published | {_SERVICE: ["not-a-key", ""]},
    }


def _event_vectors() -> list[dict[str, Any]]:
    from lakehouse.lineage.signing import verify_control_signature, verify_signature

    signer_sets = {
        "listed": ([_SERVICE, _DELEGATOR], [_DELEGATOR]),
        "no delegators": ([_SERVICE, _DELEGATOR], []),
        "a delegator not listed": ([_SERVICE], [_DELEGATOR]),
    }
    runs = [("listed", "published")]
    extra_runs = {
        "a run event its author signed": [("no delegators", "published"), ("listed", "service unpublished"), ("listed", "service publishes only junk")],
        "a run event a delegator signed for a person": [("no delegators", "published"), ("a delegator not listed", "published")],
        "a control event a delegator signed for a person": [("no delegators", "published"), ("a delegator not listed", "published")],
        "an unknown key id": [("listed", "service unpublished")],
    }
    key_sets = _key_sets()
    vectors = []
    for case in _event_cases():
        arrived = json.loads(case["arrived"])
        for signers_name, keys_name in runs + extra_runs.get(case["name"], []):
            signers, delegators = signer_sets[signers_name]
            verify = verify_control_signature if case["kind"] == "control" else verify_signature
            source = _Listed(key_sets[keys_name])
            verdict = _verdict(lambda v=verify, s=source, a=arrived, g=signers, d=delegators: v(a, source=s, signers=frozenset(g), delegators=frozenset(d)))
            vectors.append(
                case
                | {"signers": signers, "delegators": delegators, "keys": keys_name, "signed_bytes": _signed_bytes(case["kind"], arrived), "verdict": verdict}
            )
    return vectors


def _lineage_vectors() -> list[dict[str, Any]]:
    """Lineage's answer for an arrived event: unenforced, verified (and whether the catalog attests it), refused or unavailable."""
    from lance_namespace import ServiceUnavailableError

    from lakehouse.lineage.keys import PublishedKeys
    from lineage.api.fga_deps import _attested
    from lineage.core.config import Signing
    from lineage.models import UnverifiedEventError
    from lineage.services.signature import verify_arrived_event

    by_name = {case["name"]: case for case in _event_cases()}
    picked = [
        "a run event its author signed",
        "a run event a delegator signed for a person",
        "a dataset event a delegator signed for a person",
        "a changed field",
        "an event with no facets",
        "a signer that is not accepted",
    ]
    enforced_and_not: list[tuple[list[str], list[str]]] = [([_SERVICE, _DELEGATOR], [_DELEGATOR]), ([], [_DELEGATOR])]
    vectors = []
    for name in picked:
        for signers, delegators in enforced_and_not:
            for keys_name in ("published", "service unpublished"):
                listed = _key_sets()[keys_name]
                fetch: Callable[[str], Mapping[str, str]] = lambda identity, listed=listed: {"keys": ",".join(listed[identity])} if identity in listed else {}  # noqa: E731
                app: Any = SimpleNamespace(state=SimpleNamespace(published_keys=PublishedKeys(fetch)))
                signing = Signing(LINEAGE_SIGNERS=frozenset(signers), LINEAGE_DELEGATORS=frozenset(delegators))
                settings: Any = SimpleNamespace(signing=signing)
                arrived = json.loads(by_name[name]["arrived"])
                try:
                    verified = asyncio.run(verify_arrived_event(arrived, app, settings))
                except UnverifiedEventError as exc:
                    answer: dict[str, Any] = {"refused": exc.reason}
                except ServiceUnavailableError:
                    answer = {"unavailable": True}
                else:
                    named = None if verified is None else {"identity": verified.identity, "kid": verified.kid, "on_behalf_of": verified.on_behalf_of}
                    answer = ({"unenforced": True} if named is None else {"verified": named}) | {"attested": _attested(verified, settings)}
                vectors.append(
                    {"name": name, "arrived": by_name[name]["arrived"], "signers": signers, "delegators": delegators, "keys": keys_name, "answer": answer}
                )
    return vectors


def _canon_vectors() -> list[dict[str, Any]]:
    """canon-1 of a value written as JSON text, or None where it has none. The random floats are drawn from the fixture seed."""
    from lakehouse.lineage.signing import CanonError, canon_1

    texts = [
        "null",
        "true",
        "false",
        "0",
        "-0",
        "-0.0",
        "1",
        "1.0",
        "1.5",
        "5e0",
        "1E+2",
        "1e-400",
        "1e16",
        "1e22",
        "1e-05",
        "0.0001",
        "-1.5e-7",
        "5e-324",
        "1.7976931348623157e308",
        "9007199254740991",
        "-9007199254740991",
        "9007199254740992",
        "-9007199254740992",
        "9007199254740991.0",
        "9007199254740992.0",
        "9007199254740993.0",
        "123456789012345.6",
        "12345678901234567.0",
        "100000000000000000000000000000",
        "NaN",
        "Infinity",
        "-Infinity",
        "1e400",
        '"plain"',
        '"Ström 東京 😀"',
        '"\\u00e9\\ud83d\\ude00"',
        '"\\ud800"',
        '"\\udc00\\ud800"',
        '"\\ud83d\\u0041"',
        '"\\u0000\\u001f\\u007f\\u2028\\b\\f\\n\\r\\t\\"\\\\\\/"',
        "[]",
        "{}",
        '{"b": 1, "a": 2, "B": 3, "\\u00e9": 4, "e\\u0301": 5, "\\ud83d\\ude00": 6, "\\uffff": 7, "": 8}',
        '{"a": 1, "a": 2}',
        '[1, [2.5, [null, [true, [false, {"k": []}]]]]]',
        json.dumps(_nested(63)),
        json.dumps(_nested(64)),
        json.dumps({"x": _nested(63)}),
    ]
    draw = random.Random(FIXTURE_SEED)  # noqa: S311 - a fixed corpus, not a secret
    floats = []
    while len(floats) < 400:
        (number,) = struct.unpack("<d", draw.getrandbits(64).to_bytes(8, "little"))
        if number == number and abs(number) != float("inf"):
            floats.append(repr(number))
    floats += [repr(draw.uniform(-1e6, 1e6)) for _ in range(100)] + [repr(draw.randint(-(2**60), 2**60) / 2 ** draw.randint(0, 60)) for _ in range(100)]
    vectors = []
    for text in texts + floats:
        try:
            canon: str | None = canon_1(json.loads(text)).decode("utf-8")
        except CanonError:
            canon = None
        vectors.append({"text": text, "canon": canon})
    return vectors


def _key_vectors() -> dict[str, list[dict[str, Any]]]:
    """Which published entries are keys, with the key id each names, and how a list's `keys` field is split."""
    from lakehouse.lineage.signing import _PUBLIC_PREFIX, _PUBLIC_TEXT_LENGTH, _decode_nkey, _kid_of, parse_published_keys

    service = _key(_SERVICE)
    nkey = service.public_nkey
    seed_text = "S" + "U" + nkey[2:]
    tampered = nkey[:-1] + ("A" if nkey[-1] != "A" else "B")
    entries = [nkey, f" {nkey}\t", f"\x0b{nkey}\x0c", nkey.lower(), nkey[:-1], nkey + "A", tampered, seed_text, "A" + nkey[1:], "", "U" * 56]
    entries += [_key(name).public_nkey for name in (_DELEGATOR, "outsider", "svc-ingest-old")]
    keys = []
    for entry in entries:
        try:
            raw = _decode_nkey(entry, prefix=_PUBLIC_PREFIX, text_length=_PUBLIC_TEXT_LENGTH, what="NKEY public key")
        except ValueError:
            keys.append({"entry": entry, "kid": None})
            continue
        keys.append({"entry": entry, "kid": _kid_of(raw)})
    fields = ["", ",", " , ,", "a", "a,b", " a , b ,, c ", "a,\u00a0b", "\ta\n,\x0bb\x0c", f"{nkey},{nkey.lower()}"]
    return {"public_keys": keys, "published_fields": [{"field": field, "keys": parse_published_keys(field)} for field in fields]}


def signing_vectors() -> dict[str, list[Any]]:
    """The groups `rust/vectors/signing.json` holds."""
    return {
        "fixture": [{"seed": FIXTURE_SEED, "note": "every key pair here is derived from this fixture seed and protects nothing"}],
        "key_sets": [{"name": name, "published": published} for name, published in _key_sets().items()],
        "events": _event_vectors(),
        "lineage": _lineage_vectors(),
        "canon": _canon_vectors(),
        **_key_vectors(),
    }
