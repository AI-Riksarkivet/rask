"""The erasure door — and the first route on the MANAGEMENT prefix ([[LH-073]], [[LH-021]]).

**IT MOUNTS UNDER `/management/v1`, not `/v1`, and it is deliberately the first one there.** LH-021
says every rask-only route belongs off the spec surface, the way Lakekeeper separates `/management`
from `/catalog`; 42 of them sit on spec prefixes today and each has callers that a move would break.
A NEW route has none — so the prefix gets established by something with zero migration risk, and the
42 move against a door that already exists rather than against a plan.

`tests/integration/test_the_spec_surface_carries_only_spec_operations.py` is what would have caught
this going to `/v1/table/{id}/erasure` instead: the gate fails on any operation added to a spec prefix
that the spec does not define.

WHY ERASURE IS ITS OWN VERB RATHER THAN A FLAG ON DELETE. `delete_from_table` removes rows from ONE
ref and is a data operation a writer performs routinely. This removes a subject from every ref the
catalog serves, drops the tags pinning the versions that held them, and reclaims history — a
destruction of the table's past, at the owner rung. Overloading the data verb would put that behind
`can_write_data`, and the two also differ in what they must REPORT: a delete answers with a version, an
erasure has to answer with what it could not reach.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.core.identifiers import parse_identifier
from catalog.core.namespace import open_dataset
from catalog.schemas import ErasureRequest
from catalog.services.erasure import ErasureReport, erase


#: The management surface. `/management/v1` mirrors the spec's own `/v1` versioning so a management
#: client can be versioned independently of the catalog spec it sits beside.
router = APIRouter(prefix="/management/v1/table", tags=["erasure"])


@router.post("/{id}/erasure")
async def erase_subject(
    id: str,
    body: ErasureRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    authorization: Annotated[str | None, Header()] = None,
    branch: Annotated[str | None, Query(description="IGNORED: an erasure acts on every ref by construction, so naming one would narrow it.")] = None,
) -> ErasureReport:
    """Remove every row matching the predicate from every ref, then reclaim what no longer pins it.

    Owner-gated (``can_drop``) — it destroys history, which is a stronger claim than the drop rung
    guards, and there is no rung above it.

    **THE RESPONSE IS THE POINT, and a 200 is not the answer.** `complete` is False whenever a retained
    version of any ref still answers the predicate after every step ran — which happens when a retained
    branch version still stands on the files of the version it was cut from ([[LH-178]]). The residual
    is named per ref (`main@2`, `work@3`), and `pinned_by` lists what to delete to finish, in an order
    Lance accepts. A caller reporting completion to a data subject reads that field; a caller reading
    the status code reports the wrong thing.

    ``branch`` is accepted and IGNORED rather than refused, and the description says so on the wire.
    Refusing would suggest a per-ref erasure exists; honouring it would let a caller believe they had
    erased a subject when they had narrowed the operation to one ref.
    """
    segments = parse_identifier(id, settings.delimiter)
    dataset = await run_in_threadpool(open_dataset, ns, so, segments)
    return await run_in_threadpool(
        erase,
        dataset,
        table=id,
        predicate=body.predicate,
        retention=timedelta(days=body.retain_days),
    )
