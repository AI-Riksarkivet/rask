"""How a project or a warehouse is named in OpenFGA: by the uuid its creation minted, never by its id (ADR 0245).

A project's or a warehouse's id is the address every door, the console, the declaration and the events name, and a
delete frees it for another tenant to take. A tuple written on the id would hold for whoever holds it next. So each
creation mints a uuid, its registry record names it (:data:`UUID`), and OpenFGA names the object ``project:<uuid>`` or
``warehouse:<uuid>``, as tables and namespaces are named by theirs (ADR 0173): a tuple written for one creation can
never apply to another, because no create mints a uuid again.

:func:`object_of` is the ONE builder of such an object (a ruff banned-api rule admits it only at its sanctioned sites: the
catalog's resolver, ``catalog.services.tenancy``, and maintenance's rebuild of a warehouse's tenancy edge from its
record), and :func:`require_named_by_creation` is asked by every OpenFGA Write (``fga``): a tuple naming a project or a
warehouse by anything a create could take as an id is refused before it reaches the store, so a site that built an
object from an id fails closed instead of writing a tuple the next tenant would hold.

A record moves through :data:`STATE`: ``creating`` (written first, naming its uuid and who made it, before anything is
written in OpenFGA), ``live`` (once its seed stands; the only state a door resolves), ``deleting`` (marked before its
delete revokes anything), ``ended`` (stamped by the delete that ends it, which then removes it, so its id is free).
"""

from __future__ import annotations

import re
import uuid as _uuid
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Final, Literal

from lance_namespace import InternalError

from lakehouse.naming import CONTROL_ID_RE


if TYPE_CHECKING:
    from openfga_sdk.client.models import ClientTuple


#: The two kinds named by their creation.
PROJECT: Final = "project"
WAREHOUSE: Final = "warehouse"
type Kind = Literal["project", "warehouse"]
KINDS: Final = frozenset({PROJECT, WAREHOUSE})
#: The field of a project's or a warehouse's record naming the uuid its creation minted, which OpenFGA names it by.
UUID: Final = "uuid"
#: The field of a record that belongs to a project (a warehouse's, a role's, a bucket's claim, a project's maintenance
#: policy) naming the uuid of the project's creation it was made under: a record naming only the id would be the next
#: tenant's the moment a write racing the project's delete landed.
PROJECT_UUID: Final = "project_uuid"
#: The field naming where a record is in its life, and its values.
STATE: Final = "state"
CREATING: Final = "creating"
LIVE: Final = "live"
DELETING: Final = "deleting"
#: Stamped by the delete that ended the creation, under the ETag of a read that found its own mark, right before that
#: delete removes the record: a record is removed only by the one writer whose stamp landed (ADR 0245, review T2).
ENDED: Final = "ended"
type State = Literal["creating", "live", "deleting", "ended"]
STATES: Final = (CREATING, LIVE, DELETING, ENDED)
#: The shape of a uuid as :func:`new_uuid` mints it: 32 lowercase hex digits. A create refuses an id of this shape
#: (:func:`require_an_id`), so no id is ever read as a creation's uuid.
UUID_PATTERN: Final = r"[0-9a-f]{32}"
_UUID_RE: Final = re.compile(rf"{UUID_PATTERN}\Z")


def new_uuid() -> str:
    """A fresh uuid for a project's or a warehouse's creation."""
    return _uuid.uuid4().hex


def kind_of(name: str) -> Kind | None:
    """``name`` as a kind named by its creation, ``None`` for any other type."""
    return PROJECT if name == PROJECT else WAREHOUSE if name == WAREHOUSE else None


def is_uuid(value: object) -> bool:
    """Whether ``value`` has the shape of a creation's uuid."""
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


def object_of(kind: Kind, uuid: str) -> str:
    """The OpenFGA object of the creation ``uuid`` of a ``kind``.

    Raises:
        InternalError: ``uuid`` is not a creation's uuid, so a caller handed an id where the uuid belongs.
    """
    if kind not in KINDS or not is_uuid(uuid):
        raise InternalError(f"{kind}:{uuid} names no creation: a project or a warehouse is named in OpenFGA by its uuid (ADR 0245)")
    return f"{kind}:{uuid}"


def require_an_id(value: str, *, what: str) -> None:
    """Refuse a project or warehouse id shaped like a creation's uuid, so an id and a uuid are never one string.

    Raises:
        ValueError: ``value`` has a uuid's shape.
    """
    if is_uuid(value):
        raise ValueError(f"{what} {value!r} has the shape of the uuid the catalog names a creation by (32 hex digits); choose another")


def _named_by_an_id(subject: str) -> bool:
    """Whether ``subject`` (a tuple's object or user, a userset's object) names a project or a warehouse by something a
    create could take as an id: neither a uuid nor a name no id can be (the estate's own warehouse, ``lance_catalog``,
    which no create takes because an id has no ``_``)."""
    kind, _, rest = subject.partition(":")
    if kind not in KINDS:
        return False
    name = rest.partition("#")[0]
    return not is_uuid(name) and CONTROL_ID_RE.fullmatch(name) is not None


def require_named_by_creation(tuples: Iterable[ClientTuple]) -> None:
    """Refuse a Write whose tuples name a project or a warehouse by an id: the one check every OpenFGA Write asks.

    Raises:
        InternalError: A tuple names ``project:<id>`` or ``warehouse:<id>``; nothing was written.
    """
    for t in tuples:
        for subject in (str(t.object), str(t.user)):
            if _named_by_an_id(subject):
                raise InternalError(f"{subject} names a {subject.partition(':')[0]} by its id; OpenFGA names it by its creation's uuid (ADR 0245)")


def made_under(record: Mapping[str, Any], project_uuid: str | None) -> bool:
    """Whether ``record`` (a warehouse's, a role's, a bucket's claim, a project's policy) was made under the creation
    ``project_uuid`` of its project, ``None`` where none stands live. The ONE comparison of a record's project: a record a
    write racing its project's delete left names a dead creation and is no record of the tenant holding the id now."""
    return project_uuid is not None and record.get(PROJECT_UUID) == project_uuid


def warehouses_of[R: Mapping[str, Any]](records: Iterable[R], project_uuid: str | None) -> list[R]:
    """The warehouse records among ``records`` made under the creation ``project_uuid`` (:func:`made_under`): the ONE
    answer to which warehouses a project's creation holds."""
    return [r for r in records if made_under(r, project_uuid)]
