"""What the router reads off a request before it decides."""

from collections.abc import Iterator, Mapping
from typing import Annotated

from fastapi import Query, Request
from lance_namespace import InvalidInputError

from catalog.api.authz.rungs import BATCH_PATHS, CREATE_ON_PARENT_SUFFIXES, MOST_TABLES_IN_A_BATCH, WAREHOUSE_NAME_SUFFIXES, resource_for, route_suffix
from catalog.api.route_path import route_path
from catalog.core.identifiers import parse_identifier, rename_destination, require_names
from lakehouse.governed import fga_ids
from lakehouse.naming import CATALOG_DELIMITER


__all__ = [
    "DelimiterQuery",
    "batch_ids",
    "json_body_or_400",
    "json_body_or_none",
    "rename_landing",
    "require_request_names",
]


def _string_list(value: object) -> list[str] | None:
    """``value`` when it is an identifier's segments (a list of strings), else ``None``."""
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) and all(isinstance(item, str) for item in value) else None


async def json_body_or_none(request: Request) -> object:
    """The request's JSON body, or ``None`` when it holds none: the door's own validation answers a body it cannot read."""
    try:
        return await request.json()
    except ValueError:
        return None


async def json_body_or_400(request: Request) -> object:
    """The request's JSON body, for a gate that must read it to decide: one that is not JSON is the caller's malformed
    request, answered 400 before anything is decided, never the 500 an unread body would raise.

    Raises:
        InvalidInputError: The body is not JSON.
    """
    try:
        return await request.json()
    except ValueError as exc:
        raise InvalidInputError("the request body is not JSON") from exc


def batch_ids(body: Mapping[str, object]) -> Iterator[tuple[str | None, list[str]]]:
    """Each table id a batch's body names, with the operation that names it: ``None`` for an entry of ``entries``, else
    the operation's key in ``operations`` (``declare_table``, ``deregister_table``, ...). An item that is not an object,
    or an id that is not a list of strings, names nothing here; the door's own model refuses it."""
    entries = body.get("entries")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and (segments := _string_list(entry.get("id"))) is not None:
            yield None, segments
    operations = body.get("operations")
    for operation in operations if isinstance(operations, list) else []:
        if not isinstance(operation, dict):
            continue
        for kind, sub in operation.items():
            if isinstance(sub, dict) and (segments := _string_list(sub.get("id"))) is not None:
                yield str(kind), segments


def _renamed_to(body: object, source: list[str]) -> list[str] | None:
    """The identifier a rename's body moves ``source`` to, by the door's own rule (``identifiers.rename_destination``), or
    ``None`` when the body names no table name, which the door's own model refuses."""
    if not isinstance(body, dict):
        return None
    name = body.get("new_table_name")
    if not isinstance(name, str):
        return None
    return rename_destination(source, _string_list(body.get("new_namespace_id")), name)


async def rename_landing(request: Request, segments: list[str]) -> tuple[str, ...]:
    """The table a rename lands in. A body naming no destination leaves the source standing in; the door answers the 400."""
    destination = _renamed_to(await json_body_or_none(request), segments)
    return tuple(segments if destination is None else destination)


#: The spec's ``delimiter`` query parameter, declared on every route by the router's shape rung.
DelimiterQuery = Annotated[str | None, Query(description="Identifier separator. Must match the server's, which is returned in the refusal when it does not.")]


async def require_request_names(request: Request, delimiter: DelimiterQuery = None) -> None:
    """The router's shape rung: refuse, 400, a request whose identifiers no door can read (ADR 0173 §4, [[LH-526]]).

    Every door judges a request in one order: who is asking (401), whether the request is well formed (400, here), and
    whether the caller may make it (403, :func:`authorize`). A malformed request is the caller's mistake whatever they
    hold, and a 403 would send them to ask for a grant that no grant can give. Two shapes are judged:

    - ``delimiter``, how the caller split its identifier. A request naming none uses ``$``
      (``lance_docs/ns_catalog/spec.yaml:2314-2318``), the one the catalog splits on (``CATALOG_DELIMITER``), so a
      request naming another is refused rather than split wrongly;
    - every identifier a door is asked to give an object, which must be made of names (``identifiers.require_names``):
      the path id of a door that makes a table, namespace or view, a rename's destination and a batch's ids, of which
      one request names a bounded number. Any other
      door is addressed by a name that holds an object or not, and answers so.

    Router-level, never per route: a per-route check is the one the next route added forgets.

    Raises:
        InvalidInputError: ``delimiter`` is not ``$``, an identifier names nothing or holds a segment that is not a name,
            or a batch names more tables than :data:`MOST_TABLES_IN_A_BATCH`.
    """
    if delimiter is not None and delimiter != CATALOG_DELIMITER:
        raise InvalidInputError(
            f"this catalog splits identifiers with {CATALOG_DELIMITER!r}, not {delimiter!r}. "
            f"Send the identifier separated by {CATALOG_DELIMITER!r}, or omit `delimiter`."
        )
    for segments in await _identifiers_given(request):
        if not segments:
            raise InvalidInputError("an object is given a name: an empty identifier names the root namespace, which no door makes")
        require_names(segments)
    _require_a_name_inside_a_warehouse(request)


def _require_a_name_inside_a_warehouse(request: Request) -> None:
    """Refuse a path id whose shape no warehouse holds ([[LH-593]], ADR 0206).

    A name's first segment is its warehouse. So a namespace id of one segment names a warehouse, which is made, changed
    and deleted at its own doors and only read here, and a table or view lies in a namespace of a warehouse, three
    segments at least.

    Raises:
        InvalidInputError: The id is a warehouse's on a door that does not read one, or a table's or view's with no
            namespace above it.
    """
    path = route_path(request.scope)
    object_id = request.path_params.get("id")
    resource = resource_for(path)
    if object_id is None or resource not in ("namespace", "table", "materialized_view"):
        return
    segments = parse_identifier(object_id)
    if not segments:
        return
    if resource != "namespace":
        if len(segments) < 3:
            raise InvalidInputError(
                f"{resource.replace('_', ' ')} '{fga_ids.canonical_object_id(segments)}' is not inside a namespace of a warehouse: "
                f"name it <warehouse>{CATALOG_DELIMITER}<namespace>{CATALOG_DELIMITER}<name>"
            )
        return
    if len(segments) == 1 and route_suffix(path, resource, object_id) not in WAREHOUSE_NAME_SUFFIXES:
        raise InvalidInputError(
            f"'{segments[0]}' names a warehouse, which is created, changed and deleted at /management/v1/warehouses. "
            f"A namespace is named <warehouse>{CATALOG_DELIMITER}<namespace>."
        )


async def _identifiers_given(request: Request) -> list[list[str]]:
    """The identifiers ``request`` asks a door to give an object: a making door's path id, a rename's destination, a batch's ids."""
    path = route_path(request.scope)
    if path.rstrip("/") in BATCH_PATHS:
        body = await json_body_or_none(request)
        named = [segments for _kind, segments in batch_ids(body)] if isinstance(body, dict) else []
        if len(named) > MOST_TABLES_IN_A_BATCH:
            raise InvalidInputError(f"a batch names at most {MOST_TABLES_IN_A_BATCH} tables, and this one names {len(named)}")
        return named
    object_id = request.path_params.get("id")
    resource = resource_for(path)
    if object_id is None or resource is None:
        return []
    suffix = route_suffix(path, resource, object_id)
    if suffix in CREATE_ON_PARENT_SUFFIXES.get(resource, frozenset()):
        return [parse_identifier(object_id)]
    if resource == "table" and suffix == "rename":
        destination = _renamed_to(await json_body_or_none(request), parse_identifier(object_id))
        return [] if destination is None else [destination]
    return []
