"""The erasure doors: an obligation recorded, a bring-your-own job's work verified, the open ones listed ([[LH-073]], [[LH-493]]).

**AN ERASURE IS AN OBLIGATION THE CATALOG VERIFIES, NOT A REWRITE IT RUNS** (ADR 0163). The record door checks the
predicate and records the obligation; it commits nothing. A bring-your-own job holding `writer` and `maintainer`
deletes the subject's rows by row id, compacts the fragments that held them and rebuilds every user index on its own
compute, each commit judged at `version/create` (ADR 0162). The verification door judges what the job left on bytes
(`catalog.services.erasure.verify`), drops the tags pinning the subject, reclaims the history nothing pins any more, and
releases the obligation only when every head is clean and nothing but a scheduled pin release keeps a residual
(ADR 0138). The release waits on the catalog's own reading of storage, never on the job's word.

**AND ON EVERY TABLE DERIVED FROM IT** ([[LH-332]], ADR 0178). The verification names each table lineage's edge list records
as derived from the erased one, by uuid, and holds the obligation open until each holds an obligation following it,
recorded on that table's own erasure door with ``follows`` and a predicate of its own
(:mod:`catalog.services.erasure_derived`). A reader is shown the uuid only of a derived table they may see, as lineage's
derived door shows them one; the rest are counted.

**THE DOORS MOUNT UNDER `/management/v1`, not `/v1`** ([[LH-021]], ADR 0156): an erasure is rask's operation, not a Lance
namespace one, and `tests/integration/test_spec_conformance.py` fails on a rask route on a spec prefix.

WHY ERASURE IS ITS OWN VERB RATHER THAN A FLAG ON DELETE. `delete_from_table` removes rows from ONE ref and is a data
operation a writer performs routinely. An erasure removes a subject from every ref the catalog serves, drops the tags
pinning the versions that held them, and reclaims history, a destruction of the table's past at the owner rung; and the
two differ in what they must REPORT: a delete answers with a version, an erasure with what it could not reach.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from functools import partial
from typing import Annotated
from uuid import uuid4

import lance
from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps
from catalog.api.announcing import Announcer, AnnouncerDep
from catalog.api.dependencies import ClockDep, ControlEmitterDep, DoorTableDep, FgaClientDep, ProvenanceNamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings, fresh_lance_session
from catalog.core.identifiers import parse_identifier
from catalog.core.namespace import disarm_every_ref
from catalog.schemas import ErasureRequest, ErasureVerifyRequest
from catalog.services import announcement_intents, dataplane, erasure, erasure_derived, erasure_obligations, erasure_release, reclaims, table_claims
from catalog.services.announcement_intents import Announcement, ControlMessage, TagGone
from catalog.services.erasure import ErasureReport, PinRelease, SurfaceResult
from catalog.services.erasure_obligations import ErasureObligation, ErasureObligations, ErasureObligationView, ErasureVerification
from lakehouse import derived_tables
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.records import RecordExistsError
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)


#: The table-scoped doors. `/management/v1` mirrors the spec's own `/v1` versioning so a management client can be
#: versioned independently of the catalog spec it sits beside.
table_router = APIRouter(prefix="/management/v1/table", tags=["erasure"])

#: The obligations listed across tables, as `GET /management/v1/maintenance/due` lists due work.
obligations_router = APIRouter(prefix="/management/v1/erasures", tags=["erasure"])


#: Every ``user:`` subject holding ``owner`` on a ``table:<id>`` object.
type TableOwners = Callable[[str], Awaitable[list[str]]]


def get_table_owners(client: FgaClientDep, settings: SettingsDep) -> TableOwners:
    """The table's owners as the FGA model resolves them (a direct grant, a role's assignees, an owner of a
    container above it), or nobody when authorization is off: there is then no owner to tell."""

    async def _owners(object_id: str) -> list[str]:
        if (wired := fga_deps.wired(settings, client)) is None:
            return []
        return await fga.list_users(wired, relation="owner_effective", obj=object_id, qualified=True)

    return _owners


TableOwnersDep = Annotated[TableOwners, Depends(get_table_owners)]


@table_router.post("/{id}/erasure", status_code=202)
async def erase_subject(
    id: str,
    body: ErasureRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    clock: ClockDep,
    token: CurrentToken = None,
    branch: Annotated[str | None, Query(description="IGNORED: an erasure covers every ref by construction, so naming one would narrow it.")] = None,
) -> ErasureObligationView:
    """Record an erasure obligation for every row matching the predicate on every ref; 202 with the obligation.

    Owner-gated (``can_drop``): the erasure it starts destroys history, which is a stronger claim than the drop rung
    guards, and there is no rung above it.

    It commits no data. The predicate is planned on main first, so one Lance refuses answers 400 and one the store
    cannot plan answers 503, with nothing recorded. Every ref is disarmed ([[LH-245]]): `version/create` refuses a
    commit whose manifest config arms Lance's auto-cleanup, so the job's commits on an armed ref would all be refused.
    The obligation keeps the predicate's digest, never the predicate, and is due the chart's
    `erasure.deadlineDays` from now; it is listed on `GET /management/v1/erasures` and released by
    `POST /management/v1/table/{id}/erasure/{obligation_id}/verify`.

    ``follows`` names the obligation, on a table this one was derived from, that this one follows ([[LH-332]]), which its
    verification holds open until every table derived from its table holds one; one naming no recorded obligation answers
    400 with nothing recorded.

    ``branch`` is accepted and IGNORED rather than refused, and the description says so on the wire. Refusing would
    suggest a per-ref erasure exists; honouring it would let a caller believe they had erased a subject when they had
    narrowed the obligation to one ref.
    """
    segments = parse_identifier(id)
    canonical = fga.canonical_object_id(segments)
    table_uuid = table.resolved.require_uuid()
    if body.follows is not None:
        await run_in_threadpool(erasure_obligations.require_followable, settings.registry_root, settings.storage_options(), body.follows)
    dataset = await run_in_threadpool(table.open, so)
    await run_in_threadpool(erasure.plan, dataset, body.predicate)
    dataset = await run_in_threadpool(disarm_every_ref, dataset, segments)
    now = clock()
    obligation = ErasureObligation(
        obligation_id=uuid4().hex,
        table=canonical,
        table_uuid=table_uuid,
        object_id=fga.table_object(table_uuid),
        location=str(dataset.uri),
        predicate_digest=erasure_obligations.digest(body.predicate),
        retain_days=body.retain_days,
        refs=await run_in_threadpool(erasure.ref_heads, dataset),
        requested_by=f"user:{token.subject}" if token is not None else None,
        requested_at=now,
        deadline=now + timedelta(days=settings.erasure_deadline_days),
        follows=body.follows,
    )
    await run_in_threadpool(erasure_obligations.record, settings.registry_root, settings.storage_options(), obligation)
    audit(
        AuditAction.ERASURE_OBLIGATION_RECORDED,
        AuditOutcome.SUCCESS,
        subject=token.subject if token is not None else None,
        resource=obligation.object_id,
        obligation=obligation.obligation_id,
        deadline=obligation.deadline.isoformat(),
    )
    # One record per ref the obligation covers, at the head it was recorded against: "was this subject's erasure
    # owed on that branch" is answered from the trail alone, as each ref is a surface the verification judges apart.
    for ref, head in obligation.refs.items():
        audit(
            AuditAction.ERASURE_REF_OBLIGED,
            AuditOutcome.SUCCESS,
            subject=token.subject if token is not None else None,
            resource=obligation.object_id,
            obligation=obligation.obligation_id,
            ref=ref,
            version=head,
        )
    return obligation.view(now, table=canonical, hidden=())


@table_router.post("/{id}/erasure/{obligation_id}/verify")
async def verify_erasure(
    id: str,
    obligation_id: str,
    body: ErasureVerifyRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    owners: TableOwnersDep,
    provenance: ProvenanceNamespaceDep,
    client: FgaClientDep,
    clock: ClockDep,
    announcer: AnnouncerDep,
    token: CurrentToken = None,
) -> ErasureVerification:
    """Verify what the bring-your-own job left, and release the obligation when nothing but a scheduled release remains.

    Maintainer-gated (``can_maintain``): the job that rewrites holds it, and so does the table's owner. The caller
    supplies nothing the verification trusts but the predicate, and only once its digest matches the obligation's:
    a predicate nothing matches would verify clean, so one other than the requester's is refused 400. An obligation
    of another table, or one already released, answers 409.

    **THE RESPONSE IS THE POINT, and a 200 is not the answer.** `report.settled` is True only when every ref's head
    is clean on bytes (no row that answers, none behind a deletion vector, no index segment the catalog cannot prove:
    one committed since the obligation was recorded never proves itself, so the job drops its indexes and rebuilds
    them after the release) and every residual left is held by the refs `report.pinned_by` names and nothing else. The obligation
    is then released and, when anything pins a residual, the release of it is scheduled (ADR 0138): the requester and
    each table owner receive a targeted `erasure_release_scheduled` event. Otherwise the obligation stays open and
    `obligation.open_surfaces` names every surface the job still owes or the catalog could not settle.

    Each tag it drops is announced as the tag door announces a deletion, a `table_tag_deleted` control event naming the
    tag, the ref it pinned and this obligation, and `report.untagged` lists them. Those announcements, and the release's
    notices, are filed before their change (ADR 0233), so what a verification that ended part-way did not send is sent by
    the next verification of the obligation, or by the catalog's finish, under the id it was filed with.

    Every table lineage's edge list records as derived from this one is a `derived:<uuid>` surface, open until an
    obligation recorded on it follows this one ([[LH-332]]): this predicate cannot judge another table's columns. A derived
    table the caller may not see (``can_get_metadata``) is left out of both the report and the obligation they are shown,
    and those of them holding it open are counted in one `derived` surface.

    `report.complete` is evidence about every retained version of every ref, and about nothing else: a data file no
    version references that is younger than 7 days, an aborted write's residue, is neither reclaimed nor probed,
    because Lance keeps it as possibly an in-flight write's (`lance_docs/lance/guide/read_and_write.md` cleanup_old_versions
    `delete_unverified`).
    """
    segments = parse_identifier(id)
    canonical = fga.canonical_object_id(segments)
    control_root, control_options = settings.registry_root, settings.storage_options()
    obligation = await run_in_threadpool(erasure_obligations.read, control_root, control_options, obligation_id)
    table_uuid = table.resolved.require_uuid()
    dataset = await run_in_threadpool(table.open, so)
    erasure_obligations.require_verifiable(obligation, table_uuid=table_uuid, location=str(dataset.uri), predicate=body.predicate)
    # What an earlier verification of this obligation filed and did not send (it ended between a tag's delete and its
    # announcement): sent now, wherever the tag is gone.
    await announcer.finish_on(_untag_target(obligation_id))
    actor = f"user:{token.subject}" if token is not None else None
    filed: list[tuple[str, list[Announcement]]] = []

    def _file_untags(doomed: Sequence[erasure.DroppedTag]) -> None:
        # Each tag's announcement as the tag door builds it, filed before the tag is deleted, landed once it is gone.
        told = [
            Announcement(
                evidence=TagGone(location=str(dataset.uri), tag=tag.tag),
                message=ControlMessage(
                    event=CatalogControlEvent(
                        action="table_tag_deleted",
                        object_type="table",
                        object_id=fga.table_object(table_uuid),
                        object_name=canonical,
                        actor=actor,
                        extra={"tag": tag.tag, **dataplane.tag_event_ref(table.location, so, segments, tag.branch), "obligation_id": obligation_id},
                    )
                ),
            )
            for tag in doomed
        ]
        key = announcement_intents.file(control_root, control_options, door="erasure_untag", target=_untag_target(obligation_id), announcements=told)
        filed.append((key, told))

    report = await run_in_threadpool(
        erasure.verify,
        dataset,
        reopen=partial(_open_cold, dataset.uri, so),
        storage_options=so,
        table=canonical,
        predicate=body.predicate,
        retention=timedelta(days=obligation.retain_days),
        trusted=obligation.refs,
        derived=partial(erasure_derived.judge, settings, provenance, so, obligation=obligation, table=canonical),
        before_untag=_file_untags,
        condemn=reclaims.condemners(reclaims.Governance.of(table, control_root, control_options), control_root, control_options, dataset),
    )
    # Each tag the verification dropped is announced as the tag door announces a deletion ([[LH-332]]): untargeted, naming
    # the tag and the ref it pinned, beside the obligation whose verification dropped it. AFTER the drops, so a tag still
    # standing is never announced gone, and as filed before them.
    dropped = {tag.tag for tag in report.untagged}
    for key, told in filed:
        await announcer.send_filed(key, [a for a in told if isinstance(a.evidence, TagGone) and a.evidence.tag in dropped])
    if report.settled and report.pinned_by:
        # The events name the table as it is named now, which a rename since the request changed; its owners are found on
        # its uuid, which no rename changes.
        current = obligation.model_copy(update={"table": canonical})
        await _schedule_release(report, current, settings=settings, announcer=announcer, owners=owners, now=clock())
    standing = await run_in_threadpool(erasure_obligations.settle, control_root, control_options, obligation_id, report=report, now=clock())
    audit(
        AuditAction.ERASURE_OBLIGATION_VERIFIED,
        AuditOutcome.SUCCESS,
        subject=token.subject if token is not None else None,
        resource=obligation.object_id,
        obligation=obligation_id,
        state=standing.state,
        open_surfaces=",".join(surface.surface for surface in standing.open_surfaces),
    )
    hidden = await _hidden_derived([*report.surfaces, *standing.open_surfaces], late=standing.late_derived, settings=settings, client=client, token=token)
    shown = report.model_copy(update={"surfaces": erasure.shown(report.surfaces, hidden=hidden)})
    return ErasureVerification(obligation=standing.view(clock(), table=canonical, hidden=hidden), report=shown)


@obligations_router.get("")
async def list_erasure_obligations(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    clock: ClockDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    page_token: Annotated[str | None, Query(description="Continue after this obligation (the previous page's `page_token`).")] = None,
) -> ErasureObligations:
    """The erasure obligations, open and released, listed to a caller holding ``can_maintain`` on each one's table, the
    earliest deadline first: what each still owes, whether it is overdue, and the release it scheduled. Each names its
    table as the table is named now, which a rename since the request changed; one whose table is gone, its bytes with
    it, is listed to nobody. A derived table the caller may not see is counted, never named, as the verification shows it."""
    obligations = await run_in_threadpool(erasure_obligations.list_obligations, settings.registry_root, settings.storage_options())
    # Named and judged on the uuid each obligation records ([[LH-524]]) only as the page fills, so a page costs about the
    # obligations it serves or passes over.
    listed, next_token = await fga_deps.named_record_page(
        fga_deps.authorization(settings, client, token),
        table_claims.claim_store(settings),
        relation="can_maintain",
        candidates=sorted(
            ((f"{obligation.deadline.isoformat()}|{obligation.obligation_id}", obligation) for obligation in obligations), key=lambda candidate: candidate[0]
        ),
        kept_by=lambda obligation: table_claims.TableRecord(location=obligation.location, uuid=obligation.table_uuid),
        page_token=page_token,
        limit=limit,
    )
    now = clock()
    hidden = await _hidden_derived(
        [surface for _, obligation in listed for surface in obligation.open_surfaces],
        late=[key for _, obligation in listed for key in obligation.late_derived],
        settings=settings,
        client=client,
        token=token,
    )
    return ErasureObligations(obligations=[obligation.view(now, table=name, hidden=hidden) for name, obligation in listed], page_token=next_token)


async def _hidden_derived(
    surfaces: Sequence[SurfaceResult], *, late: Sequence[str], settings: Settings, client: OpenFgaClient | None, token: Caller | None
) -> frozenset[str]:
    """The derived tables ``surfaces`` and the ``late`` derived tables name that the caller may not see: none with
    authorization off, else every one the caller holds no ``can_get_metadata`` on, the rung lineage's derived door asks to
    show one, and every one keyed by a name, which no grant can reach."""
    keys = {key for surface in surfaces if (key := erasure.derived_key(surface.surface)) is not None} | set(late)
    if not keys:
        return frozenset()
    if (authz := fga_deps.authorization(settings, client, token)) is None:
        return frozenset()
    objects = sorted(fga.table_object(key) for key in keys if derived_tables.is_table_uuid(key))
    allowed = await authz.batch_check("can_get_metadata", objects) if objects else {}
    return frozenset(key for key in keys if not (derived_tables.is_table_uuid(key) and allowed.get(fga.table_object(key))))


async def _schedule_release(
    report: ErasureReport,
    obligation: ErasureObligation,
    *,
    settings: Settings,
    announcer: Announcer,
    owners: TableOwners,
    now: datetime,
) -> None:
    """ADR 0138: record the release of what ``pinned_by`` names under the obligation's id, then tell its requester and
    the table's owners.

    THE RELEASE THAT STANDS IS READ FIRST (ADR 0233). One already recorded under that id was scheduled by an earlier
    verification that did not get to settle the obligation: it is taken as it stands, while it names every pin, and the
    notices filed and sent are ITS notices, naming its deadline, to its people and to whoever must be told now (an owner
    added since), under the ids the first telling used (``erasure_release.notice``), so nobody hears it twice and nobody
    hears another deadline. None standing, the notices of the record this verification writes are FILED first, then the
    record written, then the notices sent: each lands only while that very record stands (its ``due_at``), so a person is
    never told of a release nothing will perform, nor of the deadline of a record that lost its create to another
    verification's, and one a verification that ended part-way did not send is sent by the finish, while the release tick
    waits for it. A verification that loses the create tells the people of the record that won. A record the store refuses
    is a failed `release` surface, nobody is told, and the obligation stays open. Owners that cannot be read are a failed
    `notice` surface, and the requester is still told.
    """
    try:
        holders = [subject for subject in await owners(obligation.object_id) if subject.startswith("user:")]
    except Exception as exc:  # noqa: BLE001 — the requester is told whether or not the owners can be read
        log.warning("erasure_owners_unread", extra={"table": report.table, "error": str(exc)})
        report.surfaces.append(
            SurfaceResult(surface="notice", outcome="failed", detail=f"the table's owners could not be read, so only the requester is told: {exc}")
        )
        holders = []
    requester = obligation.requested_by
    # `requested_by` is the notice's actor, `user:` and the principal key as the door verified it; a notice names its
    # subject as the OpenFGA user, the form the owners are listed in, so the requester is encoded to be told ([[LH-063]]).
    told = [fga.user_object(requester.removeprefix("user:"))] if requester else []
    notified = list(dict.fromkeys([*told, *sorted(holders)]))
    control_root, control_options = settings.registry_root, settings.storage_options()
    target = erasure_release.notice_target(obligation.obligation_id)
    standing = await run_in_threadpool(erasure_release.scheduled, control_root, control_options, obligation.obligation_id)
    if standing is None:
        record = erasure_release.new_release(
            report.pinned_by,
            release_id=obligation.obligation_id,
            table=report.table,
            table_uuid=obligation.table_uuid,
            location=obligation.location,
            requested_by=requester,
            notified=notified,
            due_at=now + timedelta(days=settings.erasure_release_grace_days),
            residual=report.residual_versions,
            retain_days=obligation.retain_days,
        )
        notices = [erasure_release.notice(record, subject, object_id=obligation.object_id) for subject in notified]
        async with announcer.announcing(door="erasure_release", target=target, announcements=notices) as filing:
            try:
                await run_in_threadpool(erasure_release.schedule, control_root, control_options, record)
            except RecordExistsError:
                # Another verification recorded the release since the read: what this one filed names a record that never
                # stood, so none of it lands, and the people are told of the record that did.
                pass
            except Exception as exc:  # noqa: BLE001 — the report still answers with what the verification found
                log.error("erasure_release_unscheduled", extra={"table": report.table, "error": str(exc)})
                report.surfaces.append(
                    SurfaceResult(surface="release", outcome="failed", detail=f"the release of {record.refs} could not be recorded, so nobody was told: {exc}")
                )
                report.settled = False
                return
            else:
                report.release = _pin_release(record, settings)
                for notice in notices:
                    await filing.send(notice)
                return
        standing = await run_in_threadpool(erasure_release.scheduled, control_root, control_options, obligation.obligation_id)
    if not _take_the_earlier_release(report, standing, settings) or standing is None:
        return
    # Its own people and whoever must be told now, each told of the record that stands, its deadline included.
    everyone = list(dict.fromkeys([*standing.notified, *notified]))
    report.release = _pin_release(standing, settings, notified=everyone)
    notices = [erasure_release.notice(standing, subject, object_id=obligation.object_id) for subject in everyone]
    async with announcer.announcing(door="erasure_release", target=target, announcements=notices) as filing:
        for notice in notices:
            await filing.send(notice)


def _take_the_earlier_release(report: ErasureReport, earlier: erasure_release.ReleaseRecord | None, settings: Settings) -> bool:
    """Settle on the release an earlier verification scheduled under the obligation's id, only while it names every pin;
    whether it did.

    A pin that release does not name would stay pinned with the obligation released, so the obligation stays open and
    the `release` surface names it; once the earlier release has run, the next verification schedules its own.
    """
    missing = sorted({pin.ref for pin in report.pinned_by} - set(earlier.refs)) if earlier is not None else []
    if earlier is not None and not missing:
        report.release = _pin_release(earlier, settings)
        return True
    if earlier is None:
        detail = "a release under this obligation's id ran meanwhile: verify again"
    else:
        detail = f"the release an earlier verification scheduled for {earlier.refs} does not name {missing}: verify again once it has run at {earlier.due_at.isoformat()}"
    report.surfaces.append(SurfaceResult(surface="release", outcome="failed", detail=detail))
    report.settled = False
    return False


def _untag_target(obligation_id: str) -> str:
    """The object a verification files the announcements of the tags it drops on: its obligation."""
    return f"erasure:{obligation_id}"


def _pin_release(record: erasure_release.ReleaseRecord, settings: Settings, *, notified: list[str] | None = None) -> PinRelease:
    return PinRelease(
        release_id=record.release_id,
        due_at=record.due_at,
        refs=record.refs,
        notified=record.notified if notified is None else notified,
        bound=erasure_release.completion_bound(record.due_at, settings.erasure_cleanup_window_days),
    )


def _open_cold(location: str, storage_options: StorageOptions) -> lance.LanceDataset:
    """The verification's handle: the LOCATION the obligation was recorded on, not the id resolved again, so the
    evidence cannot come from anywhere else; on a session no request has read through, because the shared one can
    answer a version whose files the reclaim just deleted (see `erasure.verify`)."""
    return lance.dataset(location, storage_options=storage_options.in_clear(), session=fresh_lance_session())


# One exported router, as `router.py` mounts `_module.router` for every endpoint module.
router = APIRouter()
router.include_router(table_router)
router.include_router(obligations_router)
