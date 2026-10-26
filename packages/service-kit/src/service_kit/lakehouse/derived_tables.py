"""What a table fed: every table the edge list records a run writing from it, then from those (ADR 0158, [[LH-332]]).

The edge list is lineage's ``provenance$edges``, a Lance table in the catalog's provenance namespace that lineage writes:
one row per run, input and output a successful run asserted, each table under its edge identity (a governed table's uuid,
so a rename keeps its edges; an external source's namespace-qualified name; or the bare name of a governed table an outside
producer's event named without a uuid).

**ONE WALK FOR BOTH READERS, BECAUSE THEY MUST NOT DISAGREE.** Lineage answers it on ``GET /tables/{name}/derived`` for a
caller's own grants, and the catalog's erasure verification walks it to hold an obligation until every table built from
the erased one has one of its own (ADR 0178). A requester reads the first to find the tables the second holds the
obligation to, so a rule one applied and the other did not would name tables neither answer agrees on.

An edge is followed whatever version of its input the run read: a table derived from any version of the source may hold
what an erasure removes from it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel

from service_kit.lancekit.filters import in_lists


#: The edge list's table in the provenance namespace.
EDGES: Final = "edges"

#: The edge columns a walk reads: the derived table's key, the name the run's event used for it, and when lineage
#: recorded the edge.
COLUMNS: Final = ("out_table", "out_name", "received_at")

type EdgeRows = Callable[[str], Iterable[Mapping[str, Any]]]
"""The edges a Lance filter matches, each carrying :data:`COLUMNS`; every call within one walk reads one version of the edge list."""


class DerivedWalk(BaseModel):
    """What a walk reached."""

    #: Each derived table's key, with its hops from the source: 1 for a table a run wrote from the source.
    depths: dict[str, int]
    #: The name each derived table's newest edge was recorded under.
    names: dict[str, str]
    #: False when the walk stopped at its depth with tables left to follow.
    complete: bool


def walk(rows: EdgeRows, sources: set[str], *, max_depth: int | None) -> DerivedWalk:
    """Every table derived from the table ``sources`` key, to ``max_depth`` hops, or to the last one with ``None``.

    ``sources`` are one table's edge keys: its uuid, and the names an outside producer's events keyed it by. A table among
    ``sources`` is never its own derived table, whatever cycle the edges hold.
    """
    depths: dict[str, int] = {}
    newest: dict[str, tuple[datetime, str]] = {}
    frontier, depth = set(sources), 0
    while frontier:
        found = _outputs(rows, frontier, newest) - depths.keys() - sources
        if depth == max_depth:
            return _walked(depths, newest, complete=not found)
        depth += 1
        depths.update(dict.fromkeys(found, depth))
        frontier = found
    return _walked(depths, newest, complete=True)


def is_table_uuid(key: str) -> bool:
    """Whether an edge key is a governed table's uuid rather than a name."""
    try:
        return str(uuid.UUID(key)) == key.lower()
    except ValueError:
        return False


def _outputs(rows: EdgeRows, inputs: set[str], newest: dict[str, tuple[datetime, str]]) -> set[str]:
    """The keys of the tables the edge list records a run writing from any of ``inputs``; records in ``newest`` the newest
    name each was written under."""
    found: set[str] = set()
    for where in in_lists("in_table", inputs):
        for row in rows(where):
            key = str(row["out_table"])
            found.add(key)
            named = (row["received_at"], str(row["out_name"] or key))
            if key not in newest or named[0] >= newest[key][0]:
                newest[key] = named
    return found


def _walked(depths: dict[str, int], newest: dict[str, tuple[datetime, str]], *, complete: bool) -> DerivedWalk:
    return DerivedWalk(depths=depths, names={key: newest[key][1] for key in depths if key in newest}, complete=complete)
