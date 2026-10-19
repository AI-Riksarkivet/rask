"""Lance object identifier parsing.

The REST routes carry the ``$``-delimited string identifier in the ``{id}`` path
segment. The root namespace is represented by the delimiter itself.

Object-id *canonicalization* (joining segments into the string OpenFGA stores)
lives in :mod:`service_kit.governed.fga` (``canonical_object_id`` / ``parent_namespace_id``),
so the FGA object string is defined in exactly one place and the grant + check
paths cannot drift apart. The control-plane id SHAPE (``CONTROL_ID_RE``) lives in :mod:`service_kit.lakehouse.naming` for the same reason: the
planes that CONSUME a project id decide with that rule too, and one the issuer owns alone is one the
consumers copy and then diverge from. This module owns the structural shape of a Lance object
identifier: splitting it into segments. (Parent-namespace derivation lives in
``service_kit.governed.fga``.)
"""

from __future__ import annotations

from lance_namespace import InvalidInputError


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


#: The IAM wildcard metacharacters. `*` and `?` are wildcards inside BOTH a Resource ARN and an ``s3:prefix``
#: StringLike condition, with no escape, so a value holding one that reaches the STS session policy a vend is scoped
#: by (``core.vending.build_session_policy``) WIDENS the grant to siblings — a ``foo*`` prefix authorises
#: ``bucket/foo*/*``, which matches ``foobar``. ONE tuple for every door that lets a value into that policy: a
#: segment is refused at the create door (:func:`require_safe_segments`) and a location at the register door
#: (``table_bases.require_registrable_location``), so no table prefix holds one, and the policy builder drops a
#: declared base holding one, since the table's writer chose it ([[LH-437]]). A second copy is a second thing that can
#: fall out of step with what IAM treats as a wildcard.
IAM_WILDCARD_METACHARS = ("*", "?")


def require_safe_segments(segments: list[str], *, delimiter: str) -> None:
    """Refuse an id whose segment carries the namespace delimiter or an IAM wildcard metachar (``*``/``?``) — a 400.

    The string identifier is the segments joined by the delimiter (``lance_docs/ns_catalog/spec.yaml``,
    ``delimiter``), so a segment carrying it cannot round-trip: the object would land at an id that splits
    into other segments than the ones its door decided on ([[LH-508]]). Only a door taking segments from a
    body can be handed one (the rename's ``new_table_name`` and ``new_namespace_id``); a path id is split on
    the delimiter before any door sees it.

    A segment name becomes the table's object-store prefix, and that prefix is interpolated UNESCAPED
    into the inline STS session policy that scopes vended per-table credentials. ``*`` and ``?`` are IAM
    wildcards there with no escape, so a ``foo*`` table would be handed credentials scoped to every sibling
    object under ``foo*``. Rung at SHAPE (identity -> shape -> parent exists -> authz -> conflict -> write),
    before any native write: a malformed id is not a permission problem.
    """
    for seg in segments:
        if delimiter in seg:
            raise InvalidInputError(
                f"identifier segment {seg!r} contains the namespace delimiter {delimiter!r}: an id is its segments joined "
                f"by the delimiter, so {segments!r} would land at an id naming other segments. Choose a name without it."
            )
        hit = [c for c in IAM_WILDCARD_METACHARS if c in seg]
        if hit:
            raise InvalidInputError(
                f"identifier {delimiter.join(segments)!r} has a segment {seg!r} containing the reserved "
                f"character(s) {hit}: '*' and '?' are IAM wildcards in the vended storage policy and would "
                f"widen credentials to sibling objects. Choose a name without them."
            )


def parse_identifier(id_str: str, delimiter: str) -> list[str]:
    """Split a delimited identifier into segments; empty or delimiter-only → root ``[]``."""
    if not id_str or id_str == delimiter:
        return []
    return id_str.split(delimiter)


def reconcile_body_id(path_segments: list[str], body_id: list[str] | None) -> list[str]:
    """The single spot where a request-body ``id`` meets the path ``{id}`` (spec: a differing pair is a 400,
    ``operations/index.md`` — previously the body id was silently overridden). An absent/empty body id defers
    to the path (the common case; ``[]`` also covers the root-id round-trip). The path id is what the
    router-level authz gate parsed, so a mismatch must refuse rather than pick either one."""
    if body_id and body_id != path_segments:
        raise InvalidInputError(f"request body id {body_id!r} does not match the path identifier {path_segments!r}")
    return path_segments
