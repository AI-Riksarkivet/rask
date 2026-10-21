"""How maintenance names a dataset: by the table the catalog's ``__manifest`` says it is, and by nothing else ([[LH-520]]).

A table the catalog makes lives at ``<root>/<table uuid>`` (ADR 0173 §5), so no name can be read off its location, and a
producer's ``lineage.dataset_id`` stamp is a claim, not the catalog's word ([[LH-141]]). Every maintenance reader that
acts on a dataset, or reports one the catalog does not know, asks the one rule here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from service_kit.lakehouse.base_refs import normalise
from service_kit.lakehouse.branch_layout import BRANCH_CONTAINER


class TableIds(BaseModel):
    """The catalog's table ids by location, read off every maintained root's ``__manifest``."""

    #: Normalised absolute location -> the table's ``object_id``.
    by_location: dict[str, str] = Field(default_factory=dict)
    #: ``False`` when a root's manifest could not be read: a table missing from ``by_location`` may then still exist.
    complete: bool = True

    def of(self, location: str) -> str | None:
        return self.by_location.get(normalise(location))

    def table_of(self, dataset: str) -> str | None:
        """The table a discovered dataset belongs to: the one whose row names its location, or whose branch it is.

        A branch is a whole dataset discovery lists at ``<table>/tree/<branch>``, and no row names it, so its table is the
        one whose location comes before a ``tree`` segment of its path. Every prefix is asked, since a branch's own name
        may hold ``tree`` and a registered table's location may too ([[LH-520]]).
        """
        segments = normalise(dataset).split("/")
        held = (self.by_location.get("/".join(segments[:end])) for end, segment in enumerate(segments) if segment == BRANCH_CONTAINER)
        return self.of(dataset) or next((table for table in held if table is not None), None)

    def names(self) -> set[str]:
        return set(self.by_location.values())


def under_a_declared_root(dataset: str, declared_roots: tuple[str, ...]) -> bool:
    """Whether ``dataset`` lives in a store the platform owns and opens by explicit URI, never registered ([[LH-164]]).

    The model registry is one: no ``__manifest`` row names its datasets, so they are accounted for rather than reported
    or refused as unnamed. Matched with the trailing delimiter, because a bare prefix match would declare
    ``medallion/models-scratch`` platform-owned for sharing eight characters.
    """
    return f"{normalise(dataset)}/".startswith(tuple(f"{normalise(root)}/" for root in declared_roots))
