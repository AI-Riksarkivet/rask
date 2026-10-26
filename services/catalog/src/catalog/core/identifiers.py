"""Lance object identifier parsing, and what a name is.

The REST routes carry the ``$``-delimited string identifier in the ``{id}`` path
segment. The root namespace is represented by the delimiter itself.

Joining segments back into the string identifier lives in :mod:`service_kit.governed.fga`
(``canonical_object_id`` / ``parent_namespace_id``), so one spelling serves every door, record and event. The
control-plane id SHAPE (``CONTROL_ID_RE``) lives in :mod:`service_kit.lakehouse.naming` because the planes that
CONSUME a project id decide with that rule too, and one the issuer owns alone is one the consumers copy and then
diverge from. This module owns the structural shape of a Lance object identifier: splitting it into segments, and the
rule a segment must meet to be a name (ADR 0173 §4). It also owns the key the catalog mints for a table or namespace
beside its name (ADR 0173): the property both are answered under, and the minting of a namespace's.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Final
from uuid import uuid4

from lance_namespace import InvalidInputError

from service_kit.lakehouse.naming import CATALOG_DELIMITER, UUID_PROPERTY


#: A uuid in its canonical 8-4-4-4-12 hex spelling, in either case: the name of the directory a table the catalog makes
#: lives in, directly under its namespace root (ADR 0173 §5).
_UUID_SPELLING: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def with_minted_uuid(properties: Mapping[str, str] | None) -> dict[str, str]:
    """``properties`` for a namespace this request creates, holding a uuid minted now (ADR 0173 §2)."""
    return {**(properties or {}), UUID_PROPERTY: str(uuid4())}


def is_spelled_as_a_uuid(segment: str) -> bool:
    """Whether a path segment is spelled as a uuid, as the directory a table the catalog makes is named ([[LH-520]])."""
    return _UUID_SPELLING.fullmatch(segment) is not None


#: How deep a namespace tree may nest — an EXCLUSIVE bound: this depth is the first one NOT allowed,
#: so the deepest legal namespace has ``MAX_NAMESPACE_DEPTH - 1`` segments. That is not a stylistic
#: choice. Both read walkers already compared ``>=`` (they stop descending once a parent has this many
#: segments), so a door that allowed exactly this many would mint namespaces whose TABLES no walker
#: enumerates — invisible to the cascade drop and to the trash. One number, one comparison, three sites.
#:
#: ONE constant, here rather than in an endpoint module, because
#: three separate things now depend on agreeing about it: the two read walkers
#: (`tables.py::_collect_tables`, `namespaces.py::_collect_descendants`) and — since 2026-08-15 — the
#: CREATE guard. A second copy is how F10 item 10 happened, where one walker truncated a pathological
#: tree while the other recursed into a stack overflow.
#:
#: **The ceiling is an OpenFGA limit, not a taste.** `warehouse#can_get_metadata` and
#: `namespace#can_get_metadata` are RECURSIVE (`can_get_metadata from child`), and OpenFGA abandons a
#: resolution that needs too many rewrite rules: measured against the shipped model with the real
#: evaluator, a chain of ~13 nested namespaces makes `Check(user, can_get_metadata, warehouse:X)`
#: return `Code(2002) Authorization Model resolution required too many rewrite rules` — an ERROR, not
#: a deny. The fleet fails closed on an unrecognised authz error, so that surfaces as a 503 on the
#: browse path for the WHOLE bucket, including its owners, and it is LATERAL: one deep sibling branch
#: poisons the negative check for every object in the warehouse. 8 keeps the deepest legal tree well
#: inside the evaluator's reach. (The medallion is two levels; Polaris allows 16.)
MAX_NAMESPACE_DEPTH = 8


#: The most characters a name holds (ADR 0173 §4).
MAX_NAME_LENGTH: Final = 255

#: The characters no name holds, beside a control character (ADR 0173 §4). The delimiter, because an identifier is its
#: names joined by it (`lance_docs/ns_catalog/namespace/object-relationship.md:49-68`), so a name holding it would split
#: into others ([[LH-508]]). `+`, because the catalog reads a `+` in a request's path as the space pylance's Rust
#: `RestNamespace` sends (`catalog.api.path_reading`), so a name holding one could not be addressed. `/`, which no path segment
#: carries.
_HELD_BY_NO_NAME: Final = (CATALOG_DELIMITER, "+", "/")


def require_names(segments: Sequence[str]) -> None:
    """Refuse an identifier with a segment that is not a name, 400, and never rewrite one (ADR 0173 §4, [[LH-526]]).

    A name is Unicode NFC text of 1 to :data:`MAX_NAME_LENGTH` characters holding no `$`, `+`, `/` or control character,
    and neither beginning nor ending with whitespace. Names compare exactly, case included, so one not in NFC is refused
    rather than normalized: the catalog would otherwise answer a name its client never sent. `*`, `?`, `%`, a space
    between two characters and every other letter are names, since no name reaches a storage key, a credential's policy
    or an FGA object (ADR 0173 §5, §8).

    Raises:
        InvalidInputError: A segment is not a name; the message says which and why.
    """
    for segment in segments:
        if (why := _why_not_a_name(segment)) is not None:
            raise InvalidInputError(f"identifier segment {segment!r} is not a name: {why}")


def _why_not_a_name(segment: str) -> str | None:
    """Why ``segment`` is not a name, or ``None`` when it is one."""
    if not segment:
        return "a name holds at least one character"
    if len(segment) > MAX_NAME_LENGTH:
        return f"a name holds at most {MAX_NAME_LENGTH} characters, and this one holds {len(segment)}"
    if not unicodedata.is_normalized("NFC", segment):
        return "a name is in Unicode NFC, and the catalog does not normalize one"
    if held := [char for char in _HELD_BY_NO_NAME if char in segment]:
        return f"a name holds no {' or '.join(repr(char) for char in held)}"
    if any(unicodedata.category(char) == "Cc" for char in segment):
        return "a name holds no control character"
    if segment != segment.strip():
        return "a name neither begins nor ends with whitespace"
    return None


def parse_identifier(id_str: str) -> list[str]:
    """Split a string identifier into its segments on :data:`CATALOG_DELIMITER`; empty or the delimiter alone is the root, ``[]``."""
    if not id_str or id_str == CATALOG_DELIMITER:
        return []
    return id_str.split(CATALOG_DELIMITER)


def rename_destination(source: Sequence[str], new_namespace_id: Sequence[str] | None, new_table_name: str) -> list[str]:
    """The identifier a rename of table ``source`` lands at: ``new_namespace_id``, or the source's own namespace when the
    request names none ("if not specified the table stays in the same namespace", ``lance_docs/ns_catalog/spec.yaml:4649-4653``),
    then ``new_table_name``. The one rule every reader of a rename asks, from its raw body to the backend call."""
    return [*(new_namespace_id or source[:-1]), new_table_name]


def transaction_table(segments: list[str]) -> list[str]:
    """The table a transaction id ``<table id>$<transaction>`` names, which both transaction doors act and authorize on.

    The `dir` backend reads every segment but the last as the table and the last as the transaction (its uuid or the
    version it committed), and refuses an id of one segment (measured on pylance 13.0.0), so such an id names no table.

    Raises:
        InvalidInputError: ``segments`` names no table.
    """
    if len(segments) < 2:
        raise InvalidInputError(
            f"transaction id {CATALOG_DELIMITER.join(segments)!r} names no table: a transaction is <table id>{CATALOG_DELIMITER}<transaction>"
        )
    return segments[:-1]


def reconcile_body_id(path_segments: list[str], body_id: list[str] | None) -> list[str]:
    """The single spot where a request-body ``id`` meets the path ``{id}`` (spec: a differing pair is a 400,
    ``operations/index.md`` — previously the body id was silently overridden). An absent/empty body id defers
    to the path (the common case; ``[]`` also covers the root-id round-trip). The path id is what the
    router-level authz gate parsed, so a mismatch must refuse rather than pick either one."""
    if body_id and body_id != path_segments:
        raise InvalidInputError(f"request body id {body_id!r} does not match the path identifier {path_segments!r}")
    return path_segments
