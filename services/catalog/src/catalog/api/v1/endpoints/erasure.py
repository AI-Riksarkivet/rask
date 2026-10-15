"""The erasure door — and the first route on the MANAGEMENT prefix ([[LH-073]], [[LH-021]]).

**IT MOUNTS UNDER `/management/v1`, not `/v1`, and it is deliberately the first one there.** LH-021
says every rask-only route belongs off the spec surface, the way Lakekeeper separates `/management`
from `/catalog`; 42 of them sit on spec prefixes today and each has callers that a move would break.
A NEW route has none — so the prefix gets established by something with zero migration risk, and the
42 move against a door that already exists rather than against a plan.

`tests/integration/test_spec_conformance.py::test_no_NEW_rask_route_appears_on_a_spec_prefix` fails on
any operation added to a spec prefix that the spec does not define, so this door mounted at
`/v1/table/{id}/erasure` would turn it red.

WHY ERASURE IS ITS OWN VERB RATHER THAN A FLAG ON DELETE. `delete_from_table` removes rows from ONE
ref and is a data operation a writer performs routinely. This removes a subject from every ref the
catalog serves, drops the tags pinning the versions that held them, and reclaims history — a
destruction of the table's past, at the owner rung. Overloading the data verb would put that behind
`can_write_data`, and the two also differ in what they must REPORT: a delete answers with a version, an
erasure has to answer with what it could not reach.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Annotated

import lance
from fastapi import APIRouter, Depends, Header, Query
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import ClockDep, ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.base_judge import BaseJudge
from catalog.core.config import Settings, fresh_lance_session
from catalog.core.identifiers import parse_identifier
from catalog.core.namespace import disarm_every_ref, open_dataset
from catalog.schemas import ErasureRequest
from catalog.services import erasure_release
from catalog.services.erasure import ErasureReport, PinRelease, SurfaceResult, erase
from service_kit.control_emit import ControlEmitter, emit_control
from service_kit.governed import fga


log = logging.getLogger(__name__)


#: The management surface. `/management/v1` mirrors the spec's own `/v1` versioning so a management
#: client can be versioned independently of the catalog spec it sits beside.
router = APIRouter(prefix="/management/v1/table", tags=["erasure"])


#: Every ``user:`` subject holding ``owner`` on a ``table:<id>`` object.
type TableOwners = Callable[[str], Awaitable[list[str]]]


def get_table_owners(client: FgaClientDep, settings: SettingsDep) -> TableOwners:
    """The table's owners as the FGA model resolves them (a direct grant, a role's assignees, an owner of a
    container above it), or nobody when authorization is off: there is then no owner to tell."""

    async def _owners(object_id: str) -> list[str]:
        if not settings.fga_enabled or client is None:
            return []
        return await fga.list_users(client, relation="owner", obj=object_id, qualified=True)

    return _owners


TableOwnersDep = Annotated[TableOwners, Depends(get_table_owners)]


@router.post("/{id}/erasure")
async def erase_subject(
    id: str,
    body: ErasureRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    owners: TableOwnersDep,
    clock: ClockDep,
    token: CurrentToken = None,
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
    caller reading the status code reports the wrong thing. The one status that does answer is the refusal
    before anything is touched: 400 for a predicate Lance refuses, 503 when the store cannot plan it.

    When `pinned_by` names anything, the release of it is scheduled (ADR 0138, [[LH-178]]): `release` carries
    its deadline, the refs it deletes and who was told, and the requester and each table owner receive a
    targeted `erasure_release_scheduled` event. At the deadline every named tag is deleted and every named
    branch is deleted and recreated at main's head; what they held is then reclaimed by the next erasure, or by
    the maintenance sweep's cleanup once its window passes (`release.bound`).

    `complete` can be True beside `dangling:<ref>@<n>` surfaces. Each is a version that stays listed and
    fails to read, because a branch standing on some of its files kept its manifest when the rest were
    reclaimed; no fragment of it that still reads holds the subject. It stays listed, and time travel to
    it fails, until what its detail names lets go: that branch deleted or its head rewritten.

    `complete` is evidence about every retained version of every ref, and about nothing else: a data
    file no version references that is younger than 7 days, an aborted write's residue, is neither
    reclaimed nor probed, because Lance keeps it as possibly an in-flight write's
    (`lance_docs/lance_sdk.md` cleanup_old_versions `delete_unverified`).

    A table a shallow clone resolves its files through is neither rewritten nor reclaimed, because the
    clone would break: its `compact:` and `history:` surfaces fail and `complete` is False. The subject's
    bytes outside the table's own files, an external payload, a data file under a data base or a MemWAL
    shard, are named by a `held` surface and never touched, and `complete` is False while they stay.

    ``branch`` is accepted and IGNORED rather than refused, and the description says so on the wire.
    Refusing would suggest a per-ref erasure exists; honouring it would let a caller believe they had
    erased a subject when they had narrowed the operation to one ref.
    """
    segments = parse_identifier(id, settings.delimiter)
    dataset = await run_in_threadpool(open_dataset, ns, so, segments)
    # Every ref disarmed before the first delete ([[LH-245]]): each delete and compaction below is a commit, and
    # on an armed ref Lance would reclaim inside it, ignoring `retain_days`, with nothing in the report.
    dataset = await run_in_threadpool(disarm_every_ref, dataset, segments)
    # The #114 pre-pass `version/create` and the GC door run, for the same reason: the evidence that another
    # dataset resolves its files through this one lives only on that dataset. Same bound too, one listing
    # of the table's parent (`sibling_base_refs`), and a partial map is logged and used, as they do.
    protected = await run_in_threadpool(BaseJudge.from_settings(settings).sibling_base_refs, dataset.uri, so)
    if protected.unreadable:
        log.warning("erasure_base_refs_incomplete", extra={"location": dataset.uri, "unreadable": len(protected.unreadable)})
    report = await run_in_threadpool(
        erase,
        dataset,
        reopen=partial(_open_cold, dataset.uri, so),
        storage_options=so,
        protected=protected,
        table=id,
        predicate=body.predicate,
        retention=timedelta(days=body.retain_days),
    )
    if report.pinned_by:
        object_id = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
        requester = f"user:{token.sub}" if token is not None else None
        await _schedule_release(report, settings, control, owners, location=str(dataset.uri), object_id=object_id, requester=requester, now=clock())
    return report


async def _schedule_release(
    report: ErasureReport,
    settings: Settings,
    control: ControlEmitter,
    owners: TableOwners,
    *,
    location: str,
    object_id: str,
    requester: str | None,
    now: datetime,
) -> None:
    """ADR 0138: record the release of what ``pinned_by`` names, then tell the requester and the table's owners.

    The record is written FIRST, so a person is never told of a release nothing will perform; a record the
    store refuses is a failed `release` surface and nobody is told. Owners that cannot be read are a failed
    `notice` surface, and the requester is still told. Both keep ``complete`` False, which it already is
    while anything pins a residual.
    """
    try:
        holders = [subject for subject in await owners(object_id) if subject.startswith("user:")]
    except Exception as exc:  # noqa: BLE001 — the requester is told whether or not the owners can be read
        log.warning("erasure_owners_unread", extra={"table": report.table, "error": str(exc)})
        report.surfaces.append(
            SurfaceResult(surface="notice", outcome="failed", detail=f"the table's owners could not be read, so only the requester is told: {exc}")
        )
        holders = []
    notified = list(dict.fromkeys([*([requester] if requester else []), *sorted(holders)]))
    due_at = now + timedelta(days=settings.erasure_release_grace_days)
    record = erasure_release.new_release(
        report.pinned_by, table=report.table, location=location, object_id=object_id, requested_by=requester, notified=notified, due_at=due_at
    )
    try:
        await run_in_threadpool(erasure_release.schedule, settings.registry_root, settings.storage_options(), record)
    except Exception as exc:  # noqa: BLE001 — the report still answers with what the erasure did
        log.error("erasure_release_unscheduled", extra={"table": report.table, "error": str(exc)})
        report.surfaces.append(
            SurfaceResult(surface="release", outcome="failed", detail=f"the release of {record.refs} could not be recorded, so nobody was told: {exc}")
        )
        return
    report.release = PinRelease(
        release_id=record.release_id,
        due_at=due_at,
        refs=record.refs,
        notified=notified,
        bound=erasure_release.completion_bound(due_at, settings.erasure_cleanup_window_days),
    )
    for subject in notified:
        await emit_control(
            control,
            action="erasure_release_scheduled",
            object_type="table",
            object_id=object_id,
            actor=requester,
            extra={"subject": subject, "release_id": record.release_id, "refs": record.refs, "due_at": due_at.isoformat()},
        )


def _open_cold(location: str, storage_options: dict[str, str]) -> lance.LanceDataset:
    """The verification's handle: the LOCATION the erasure ran on, not the id resolved again, so the
    evidence cannot come from anywhere else; on a session no request has read through, because the
    shared one can answer a version whose files the reclaim just deleted (see `erase`)."""
    return lance.dataset(location, storage_options=storage_options, session=fresh_lance_session())
