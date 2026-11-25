"""User and object ids: how a principal, a grantee, a table and a namespace are named to OpenFGA, and which object ids
OpenFGA accepts at all.

A subject becomes a ``user`` only through :func:`user_object`, and a table or namespace is held by its uuid
(:func:`table_object`, :func:`namespace_object`; ADR 0173 §8), so no name reaches OpenFGA as an object.
"""

import unicodedata
from typing import Final
from urllib.parse import unquote

from lance_namespace import InvalidInputError

from lakehouse.governed.fga_model import model_types
from lakehouse.governed.principal import Principal
from lakehouse.naming import CATALOG_DELIMITER


#: The model's typed wildcard: `managed_access` is typed `[user:*, role:*]`, and a grant API may name it as written.
USER_WILDCARD: Final = "user:*"


def user_object(subject: str) -> str:
    """The OpenFGA user a subject is (`Principal.fga_user`): `user:` and the principal key, percent-encoded.

    The one place a subject becomes a `user` id; :func:`subject_of` is its inverse, for ids read back from the store. A
    subject that is not a principal (`<idp-id>~<subject>`) is refused `InvalidInputError`, so no tuple, seed or check
    can name a bare name nobody can authenticate as ([[LH-063]]).
    """
    try:
        return Principal.parse(subject).fga_user
    except ValueError as exc:
        raise InvalidInputError(f"{subject!r} is not a principal: {exc}") from exc


def subject_of(user_id: str) -> str:
    """The subject a ``user`` object id names (the id after ``user:``): the inverse of :func:`user_object`."""
    return unquote(user_id)


def grantee_object(grantee: str) -> str:
    """The OpenFGA user a grant names: an object of another model type passes, a principal becomes its ``user``.

    ``role:stewards#assignee``, ``project:p`` and the typed wildcard ``user:*`` name objects of
    the model's own grammar and pass as written. ``user:<id>`` is an OpenFGA user id, the form the store hands back,
    so it is decoded and encoded again (:func:`subject_of`, :func:`user_object`): a stored id a reader echoes back names
    the tuple it came from. A bare value is a principal key (``<idp-id>~<subject>``), a service account's included, and
    is encoded once. Anything that names no principal is refused ``InvalidInputError``.
    """
    if grantee == USER_WILDCARD:
        return grantee
    kind, typed, rest = grantee.partition(":")
    if typed and kind != "user" and kind in model_types():
        return grantee
    return user_object(subject_of(rest) if typed and kind == "user" else grantee)


def is_object_id(obj: str) -> bool:
    """Whether OpenFGA v1.18.3 accepts ``obj`` as an object id at all.

    Check refuses the whole request when an object has a space or other ASCII whitespace or is outside
    2..256 runes (``CheckRequestTupleKey.object``, ``^[^\\s]{2,256}$``), and refuses the item when it is
    not ``<type>:<id>`` with exactly one ``:``, no ``#`` and no control character (``tuple.IsValidObject``),
    or when its id is the typed wildcard ``*`` (an object cannot be one). Write applies the same grammar, so
    no tuple can name such an object: "not granted" is its true answer, not a guess. Measured against v1.18.3
    through Dagger (2026-09-27): a control character, a second ``:`` and ``#`` fail the item, a space, a tab
    and 257 runes fail the request; measured on the suite's v1.18.3 Check (2026-11-22): ``<type>:`` (an empty
    id) and ``<type>:*`` answer 400 ``validation_error``, while ``a*``, ``**``, a no-break space and an em
    space are accepted.
    """
    kind, _, identifier = obj.partition(":")
    return (
        2 <= len(obj) <= 256
        and bool(kind)
        and bool(identifier)
        and identifier != "*"
        and ":" not in identifier
        and "#" not in obj
        and " " not in obj
        and not any(unicodedata.category(char) == "Cc" for char in obj)
    )


# --------------------------------------------------------------------------- #
# Object-id canonicalisation
# --------------------------------------------------------------------------- #


def canonical_object_id(id_segments: list[str] | str) -> str:
    """Return the delimited id of a name: a table's or namespace's catalog id (``a$b``), or the id of an object no uuid
    keys (a transaction, a classification label).

    A table's or namespace's FGA object is its uuid (:func:`table_object`), never this. Pass either the already-joined
    id string (returned unchanged) or the list of path segments (joined with :data:`CATALOG_DELIMITER`).
    """
    if isinstance(id_segments, str):
        return id_segments
    return CATALOG_DELIMITER.join(id_segments)


def parent_namespace_id(table_id: list[str] | str) -> str | None:
    """Derive a table's parent namespace id from its id (all segments but the last).

    Returns ``None`` for a single-segment / root-level table (no parent namespace
    to inherit from). Accepts segments or an already-joined id string so callers
    can pass whichever they hold; the result is the canonical delimited id. Empty
    segments are dropped first, so a delimiter-only / blank id (the root, e.g. ``"$"``)
    collapses to ``None`` and agrees with ``parse_identifier`` — keeping the grant and
    check paths byte-for-byte aligned on the root id.
    """
    raw = table_id.split(CATALOG_DELIMITER) if isinstance(table_id, str) else table_id
    segments = [s for s in raw if s]
    if len(segments) <= 1:
        return None
    return canonical_object_id(segments[:-1])


def table_object(uuid: str) -> str:
    """The FGA object a table's grants are held by: ``table:<uuid>``, the uuid the catalog minted for it (ADR 0173 §8).

    No name reaches OpenFGA. A name is the spec's identifier and changes on a rename, while the uuid is the table's for
    its life and a table created again under a dropped name is given a new one, so nothing granted on the old table
    reaches it ([[LH-524]]).
    """
    return f"table:{uuid}"


def namespace_object(uuid: str) -> str:
    """The FGA object a namespace's grants are held by: ``namespace:<uuid>``, its ``rask.uuid`` (ADR 0173 §8)."""
    return f"namespace:{uuid}"


def object_of(kind: str, uuid: str) -> str:
    """:func:`namespace_object` for a ``namespace``, else :func:`table_object`: the object a ``kind``'s uuid is held by."""
    return namespace_object(uuid) if kind == "namespace" else table_object(uuid)


def project_object(project_id: str) -> str:
    """The FGA object a project's grants are held by: ``project:<id>``, the id its registry record is kept under."""
    return f"project:{project_id}"


def warehouse_object(warehouse_id: str) -> str:
    """The FGA object a warehouse's grants are held by: ``warehouse:<id>``, the id its registry record is kept under."""
    return f"warehouse:{warehouse_id}"


def addressed_object(fga_type: str, id_segments: list[str] | str) -> str:
    """``<type>:<canonical-id>`` for an object addressed by its id.

    The FGA object of a type no name is minted a uuid for (a transaction's opaque id, a classification label, a
    materialized view), and the NAME of a table or namespace in an audit record or a refusal: a judgement made on an
    address before, or without, an object. A table's or namespace's grants are held by its uuid (:func:`table_object`,
    :func:`namespace_object`), never by this.
    """
    return f"{fga_type}:{canonical_object_id(id_segments)}"


def names_someone(user: str) -> bool:
    """Whether a tuple's ``user`` names someone a notifier can address: a person or a group of them.

    ``user:<principal>`` is a person; ``role:<id>#assignee`` is a userset a notifier expands into people (the decision
    door's list-users). Every other user a revoke removes is an object-to-object edge (``project``, ``parent``,
    ``child``), which names no one, so a ``grant_revoked`` for it would address an inbox named after an object.
    """
    return user.startswith("user:") or "#" in user


__all__ = [
    "USER_WILDCARD",
    "addressed_object",
    "canonical_object_id",
    "grantee_object",
    "is_object_id",
    "names_someone",
    "namespace_object",
    "object_of",
    "parent_namespace_id",
    "project_object",
    "subject_of",
    "table_object",
    "user_object",
    "warehouse_object",
]
