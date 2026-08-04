"""Lance object identifier parsing.

The REST routes carry the ``$``-delimited string identifier in the ``{id}`` path
segment. The root namespace is represented by the delimiter itself.

Object-id *canonicalization* (joining segments into the string OpenFGA stores)
lives in :mod:`service_kit.governed.fga` (``canonical_object_id`` / ``parent_namespace_id``),
so the FGA object string is defined in exactly one place and the grant + check
paths cannot drift apart. This module owns the structural shape of an identifier:
splitting it into segments. (Parent-namespace derivation lives in ``service_kit.governed.fga``.)
"""

from __future__ import annotations

import re

from lance_namespace import InvalidInputError


#: The shape a control-plane id (project, warehouse, bucket) must have: DNS-safe — lowercase
#: alphanumeric plus hyphens, 3-63 characters, never starting or ending with a hyphen. It doubles as an
#: S3 bucket name, a registry filename and an OpenFGA object id, so the strictest of those wins.
#:
#: **Anchored with ``\Z``, not ``$``, and that is load-bearing.** Python's ``$`` also matches just BEFORE
#: a trailing newline, so ``"acme\n"`` satisfied a ``$``-anchored copy of this rule. That id then became a
#: registry filename AND an FGA object id — and OpenFGA rejects whitespace in the latter, so the registry
#: record landed and the tuple write failed: precisely the record-without-tuples drift Decision 1 exists to
#: prevent. Found 2026-08-04 in an adversarial review, live in two of the three copies of this pattern.
#:
#: It is ONE constant because it was three copies that had already diverged. A shape rule duplicated per
#: endpoint is a rule that holds until someone fixes one site.
ID_PATTERN = r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
CONTROL_ID_RE = re.compile(rf"^{ID_PATTERN}\Z")


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
