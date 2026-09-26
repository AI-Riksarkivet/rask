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

import logging
from datetime import timedelta
from functools import partial
from typing import Annotated

import lance
from fastapi import APIRouter, Header, Query
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.core.config import fresh_lance_session
from catalog.core.identifiers import parse_identifier
from catalog.core.namespace import open_dataset
from catalog.schemas import ErasureRequest
from catalog.services.erasure import ErasureReport, erase
from service_kit.lakehouse import base_refs


log = logging.getLogger(__name__)


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
    is named per ref (`main@2`, `work@3`) with what keeps it: `pinned_by` the tags, and a branch only
    when its head stands on the residual's files, in an order Lance accepts; `held_by_retention` what
    the `retain_days` window keeps. A caller reporting completion to a data subject reads `complete`; a
    caller reading the status code reports the wrong thing.

    `complete` can be True beside `dangling:<ref>@<n>` surfaces. Each is a version that stays listed and
    fails to read, because a branch standing on some of its files kept its manifest when the rest were
    reclaimed; no fragment of it that still reads holds the subject. It stays listed, and time travel to
    it fails, until what its detail names lets go: that branch deleted or its head rewritten.

    `complete` is evidence about every retained version of every ref, and about nothing else: a data
    file no version references that is younger than 7 days, an aborted write's residue, is neither
    reclaimed nor probed, because Lance keeps it as possibly an in-flight write's
    (`lance_docs/lance_sdk.md` cleanup_old_versions `delete_unverified`).

    A table a shallow clone resolves its files through is neither rewritten nor reclaimed, because the
    clone would break: its `compact:` and `history:` surfaces fail and `complete` is False.

    ``branch`` is accepted and IGNORED rather than refused, and the description says so on the wire.
    Refusing would suggest a per-ref erasure exists; honouring it would let a caller believe they had
    erased a subject when they had narrowed the operation to one ref.
    """
    segments = parse_identifier(id, settings.delimiter)
    dataset = await run_in_threadpool(open_dataset, ns, so, segments)
    # The #114 pre-pass the compact and GC doors run, for the same reason: the evidence that another
    # dataset resolves its files through this one lives only on that dataset. Same bound too, one listing
    # of the table's parent (`sibling_base_refs`), and a partial map is logged and used, as they do.
    protected = await run_in_threadpool(base_refs.sibling_base_refs, dataset.uri, so)
    if protected.unreadable:
        log.warning("erasure_base_refs_incomplete", extra={"location": dataset.uri, "unreadable": len(protected.unreadable)})
    return await run_in_threadpool(
        erase,
        dataset,
        reopen=partial(_open_cold, dataset.uri, so),
        storage_options=so,
        protected=protected,
        table=id,
        predicate=body.predicate,
        retention=timedelta(days=body.retain_days),
    )


def _open_cold(location: str, storage_options: dict[str, str]) -> lance.LanceDataset:
    """The verification's handle: the LOCATION the erasure ran on, not the id resolved again, so the
    evidence cannot come from anywhere else; on a session no request has read through, because the
    shared one can answer a version whose files the reclaim just deleted (see `erase`)."""
    return lance.dataset(location, storage_options=storage_options, session=fresh_lance_session())
