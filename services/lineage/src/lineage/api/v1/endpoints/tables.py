"""What was derived from a governed table, read off the edge list (ADR 0158, LH-468).

An erasure follows a subject's source into every table built from it ([[LH-332]]): the tables written by the runs
that read it, and by the runs that read those. Gated on ``can_get_metadata`` for the source, as every read naming a
table is; a derived table the caller may not see is dropped from the answer, while the walk still passes through it
to the tables beyond.

AN ANSWER THAT HIDES A TABLE SAYS IT IS INCOMPLETE. An erasure that cannot see every table built from its source must
not read the answer as all of them, so a hidden table turns ``complete`` false, as a walk that stopped short does. That
tells the caller that something it may not see exists, and nothing about which table it is.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from lineage.api.dependencies import BearerDep, NamesDep, ProvenanceDep, SettingsDep
from lineage.api.fga_deps import FilterDep, require_metadata_access
from lineage.schemas import Derived
from lineage.services.provenance import MAX_DERIVED_DEPTH
from service_kit.governed import fga


router = APIRouter(prefix="/tables", tags=["query"], dependencies=[Depends(require_metadata_access)])


@router.get("/{name}/derived")
async def get_derived(
    name: str,
    provenance: ProvenanceDep,
    names: NamesDep,
    bearer: BearerDep,
    datasets: FilterDep,
    settings: SettingsDep,
    depth: Annotated[int, Query(ge=1, le=MAX_DERIVED_DEPTH)] = MAX_DERIVED_DEPTH,
) -> Derived:
    """Every table derived from ``name`` the caller may see, nearest first, to ``depth`` hops.

    ``name`` resolves to the uuid of the table it names now, and the walk starts there, so it follows what the table
    fed under every name it has had ([[LH-522]]). It starts from the name too, which an outside producer's event keys
    the table by when its credential could not resolve it: an erasure that reaches further than the table's own
    history is the safe direction. ``complete`` is false when the walk stopped short or a derived table is hidden from
    the caller.
    """
    resolved = (await names.table(name, bearer=bearer)).table_uuid
    tables, walked = await provenance.derived({name} | ({resolved} if resolved else set()), max_depth=depth)
    # A derived table is shown on the uuid its edges recorded ([[LH-524]]); one recorded without a uuid names no object.
    visible = await datasets.visible([fga.table_object(derived.uuid) for derived in tables if derived.uuid])
    shown = [derived for derived in tables if derived.uuid and fga.table_object(derived.uuid) in visible] if settings.fga_enabled else tables
    return Derived(table=name, uuid=resolved, derived=shown, complete=walked and len(shown) == len(tables))
