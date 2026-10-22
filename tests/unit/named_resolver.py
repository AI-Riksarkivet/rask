"""A resolver whose every name holds an object with a uuid derived from the name, for a test that drives an authorization
seam of the catalog directly rather than through a request.

Through a request the catalog resolves each name once (``catalog.api.dependencies.Resolver``) through the namespace
connection it routes to. A test whose claim is about which relation is asked, not about the resolution, passes this
one: ``uuid_of`` names the uuid it answers, so an assertion can say which object a check reached, and ``absent`` names
the names that hold no object.
"""

from __future__ import annotations

import uuid
from typing import Any, override

from lance_namespace import LanceNamespace

from catalog.api.dependencies import Resolver
from catalog.services.table_claims import ResolvedTable


def uuid_of(kind: str, segments: list[str]) -> str:
    """The uuid :class:`NamedResolver` answers for ``kind`` ``segments``."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kind}:{'$'.join(segments)}"))


class NamedResolver(Resolver):
    """Every name but ``absent`` holds an object, ``<kind>:<uuid_of(kind, segments)>``, and the trash holds one record
    for each name in ``in_trash``, under the same uuid; no store is read. Every public method is answered here, so
    nothing inherited reads the request or settings a real resolver is built with."""

    def __init__(self, absent: frozenset[tuple[str, ...]] = frozenset(), in_trash: frozenset[tuple[str, ...]] = frozenset()) -> None:
        self._absent = absent
        self._in_trash = in_trash
        self._admitted_trash: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}

    @override
    async def table(self, segments: list[str], *, ns: LanceNamespace | None = None) -> ResolvedTable:
        return (
            ResolvedTable(segments=segments)
            if tuple(segments) in self._absent
            else ResolvedTable(segments=segments, location=f"memory://{uuid_of('table', segments)}")
        )

    @override
    async def namespace_uuid(self, segments: list[str]) -> str | None:
        return None if not segments or tuple(segments) in self._absent else uuid_of("namespace", segments)

    @override
    async def object(self, kind: str, segments: list[str]) -> str | None:
        if tuple(segments) in self._absent:
            return None
        return f"{kind}:{uuid_of(kind, segments)}"

    @override
    async def trashed(self, kind: str, segments: list[str]) -> list[dict[str, Any]]:
        if tuple(segments) not in self._in_trash:
            return []
        return [{"kind": kind, "id": "$".join(segments), "uuid": uuid_of(kind, segments)}]

    @override
    def admit_trashed(self, kind: str, segments: list[str], records: list[dict[str, Any]]) -> None:
        self._admitted_trash[(kind, tuple(segments))] = records

    @override
    async def admitted_trash(self, kind: str, segments: list[str]) -> list[dict[str, Any]]:
        return self._admitted_trash.get((kind, tuple(segments)), await self.trashed(kind, segments))
