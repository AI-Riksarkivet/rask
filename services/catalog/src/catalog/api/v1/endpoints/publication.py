"""`POST /management/v1/table/{id}/publish` — the one door through which data becomes consumable.

The publication contract, ruled by the owner 2026-08-04 (`c6c23407`) as D-R1/D-R2/D-R3. A commit makes a version READABLE; publishing makes it
READY. The gate runs against the committed version and only a pass advances the `published` tag, so
a consumer reading through the pointer never sees a batch that failed.

**THE TAG IS THE GATE (ADR 0156 C1, [[LH-466]]).** Readers of a governed tier read `@published`. When the
project's declared gate has review on, a version that breaches it is HELD: `published` stays, the hold is
listed on `GET /management/v1/promotions`, and a `can_promote` holder answers it on
`POST /management/v1/table/{id}/promotion/approve` (`published` moves to the held version, the spec's
`UpdateTableTag`) or `.../promotion/reject` (main is restored to the published version, the spec's
`RestoreTable`). Every move of `published` is announced as a `publish_table` lineage run, which the
notifications plane delivers to the project's watchers.

It is an endpoint, not a library call, because every writer must publish identically — the ingest
plane, a Ray job, a backfill script, a person with credentials. A contract each writer implements
for itself is a contract that drifts per writer, which is the failure § D was written about.

**Gated at `can_update_tag`**, the same rung as `tags/update`, because that is exactly what this
does to the ref plane. A lower bar would make `publish` a way for a plain data writer to move a tag
they are not permitted to move directly. The quality gate and the FGA gate are orthogonal and both
apply: FGA decides whether the CALLER may publish, the assertions decide whether the DATA may be.

The heavy work — opening the dataset, scanning for the assertions, moving the tag — is blocking
Lance/object-store IO and runs in a threadpool, like every other Lance-touching route here.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Header, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, InternalError, InvalidTableStateError, RestoreTableRequest

from catalog.api import lineage_deps
from catalog.api.dependencies import (
    ClockDep,
    ControlEmitterDep,
    DoorTableDep,
    FgaClientDep,
    LineageEmitterDep,
    ObjectUuidsDep,
    SettingsDep,
    StorageOptionsDep,
)
from catalog.api.fga_deps import authorization, require_relation
from catalog.api.pagination import paginate
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.core.lineage_emit import PUBLISH_TABLE, RESTORE_TABLE, LineageEmitter, UuidReader, emit_write_event, is_person_subject
from catalog.schemas import PromotionDecision, PromotionDecisionRequest, PromotionHold, PromotionsListResponse, PublishRequest, PublishResult
from catalog.services import dataplane, promotions, publication, table_claims, warehouses
from catalog.services.table_claims import DoorTable
from service_kit.control_emit import ControlEmitter, emit_control
from service_kit.governed import fga
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.records import RecordChangedError


if TYPE_CHECKING:
    from datetime import datetime


#: THE MANAGEMENT SURFACE ([[LH-021]]). These are rask's own operations, not Lance namespace ones —
#: a spec client discovering them on `/v1/table` meets verbs the document never defines. They mount at
#: `/management/v1` and inherit the same three rungs from `api/v1/router.py`.
table_router = APIRouter(prefix="/management/v1/table", tags=["publication"])
#: The holds awaiting a decision, across tables.
promotions_router = APIRouter(prefix="/management/v1/promotions", tags=["publication"])


class ProjectSource(Protocol):
    """The one method `publication_extra` needs. `LineageEmitter` satisfies it and carries the
    request-scoped binding cache; demanding the whole emitter would ask for six more it does not use."""

    async def project_for(self, top_ns: str) -> str | None: ...


#: NO LOCAL COPY OF "is this a person" LIVES HERE. `catalog.core.lineage_emit.is_person_subject` is the
#: one definition, and its own docstring says why: "the two disagreeing is the whole failure mode".
#: This module briefly declared a weakened set — `{"", "*", "user:*"}` — which omitted the ROLE
#: LITERALS the canonical set carries (`ray`, `data_eng`, `analyst`, `anon`, `system`, `service`).
#: Measured on that version, `publication_originator("data_eng", …)` returned `"data_eng"`, so a failed
#: gold stage still wrote a row into an inbox actor named after a role — verbatim the symptom this
#: door was added to eliminate (`.claude/skills/rask-notifications`, trap 1).


def publication_originator(claimed: str, token: Caller | None) -> str:
    """The PERSON this publication is for, or `""` when it is for nobody.

    THE CATALOG IS THE ONLY COMPONENT THAT CAN ANSWER THIS, which is why the decision lives here
    rather than at the consumer. `table_published` is what wakes the next cascade hop, and the hop
    needs to know whether the caller was a person or a service — a question only the door that
    authenticated them can answer: `catalog/api/security.py` answers a `ServicePrincipal` for a machine,
    a service account the cluster vouched for or a Dex client signed in for itself (ADR 0181), and a
    `PersonPrincipal` for a person.

    Precedence, and both halves are load-bearing:

    * A SERVICE caller's `claimed` wins. A service principal publishes as itself, so the actor is a
      role, not an address; the human is only on the request body, carried there by the pipeline
      running on their behalf.
    * A PERSON's own principal wins over anything they claimed. Someone publishing by hand IS the
      originator, and honouring a body field over a verified principal would let a caller redirect a
      notification into somebody else's inbox.

    Neither is trusted as authorization: the notifications plane re-derives every recipient's
    visibility at delivery, so the worst a forged claim achieves is a row in the inbox of someone who
    can already read the run's outputs.

    `""` when nobody is named — a reconcile, a backfill or a cron sweep genuinely has no person behind
    it, and the honest answer is silence. Per the skill, a state change naming nobody is
    UNDELIVERABLE, not under-delivered, and an empty or role-shaped address is strictly worse than
    none because it looks delivered.
    """
    if token is None:
        # OIDC off: nobody was authenticated, so the only identity in the request is the claim.
        return _addressable(claimed)
    # The principal's TYPE decides, never a claim: a `ServicePrincipal` is built only for a machine a verifier
    # proved, and a person's token cannot pass for one by carrying an extra field (`idp_identity`, ADR 0181).
    if isinstance(token, ServicePrincipal):
        return _addressable(claimed)
    return _addressable(token.subject)


def _addressable(subject: str) -> str:
    """`subject` if it names a person, else `""` — the person rule the control lane applies.

    The optional `user:` prefix is STRIPPED rather than refused, because both spellings reach this
    field honestly: a pipeline carries a bare principal key while a person's verified actor is written
    `user:<principal key>`. Refusing one of them would drop a real person for a wire-shape difference,
    silently and with a 200.
    """
    # STRIP BEFORE CHECKING. `is_person_subject` treats a `user:`-prefixed value as an FGA object id
    # rather than a subject, so checking first would refuse `user:dex~<id>` — a real person, written in
    # the spelling an actor arrives in. Strip, then let the canonical check judge the principal key.
    candidate = subject.strip().removeprefix("user:").strip()
    return candidate if is_person_subject(candidate) else ""


async def publication_extra(
    lineage: ProjectSource,
    segments: list[str],
    *,
    from_version: int | None,
    to_version: int | None,
    location: str,
    accepted: list[str] | None = None,
    cascade_id: str = "",
    originator: str = "",
) -> dict[str, Any]:
    """The `table_published` payload: the version RANGE, the vended location, and the TENANT.

    The tenant is here because the catalog is the only component that can answer it — the binding
    (namespace → warehouse → project) is a registry read, and `PROJECT_PATTERN` permits `-` inside a
    project id, so no string rule can recover `acme` from `acme-bronze`. The medallion's publication
    head took segment 0 and got the namespace instead. Resolved through the emitter so this names the
    same tenant the lineage facet does, off the same request-scoped cache.

    `project` is OMITTED rather than empty when unresolved: a single-tenant estate has no tenant, and
    a consumer must be able to tell that from one named "". Resolution cannot raise
    (`LineageEmitter.project_for` swallows), which is what lets this run after the tag has moved.
    """
    extra: dict[str, Any] = {"from_version": from_version, "to_version": to_version, "location": location}
    # A publication that WAIVED a finding is a different governance fact from a clean one, and the
    # audit viewer must be able to tell them apart. Omitted when empty so an ordinary publish is
    # byte-identical to before.
    if accepted:
        extra["accepted_assertions"] = list(accepted)
    # The batch identity, echoed verbatim (§8 change 9). OMITTED when empty, on the same rule as
    # `project` below: a consumer must be able to tell "no batch identity" from one named "".
    if cascade_id:
        extra["cascade_id"] = cascade_id
    # THE PERSON, resolved by `publication_originator` before it gets here. Omitted when empty on the
    # same rule as `cascade_id` and `project`: the publication head reads this field and puts it on the
    # next tier's trigger, so a `""` would be threaded down to a failing stage and addressed to an
    # inbox actor named "". A consumer must be able to tell "nobody" from somebody named nothing.
    if originator:
        extra["originator"] = originator
    project = await lineage.project_for(segments[0]) if segments else None
    # STAMPED ONLY WHEN THE ID ACTUALLY CARRIES THAT TENANT ([[LH-137]], owner ruling 2026-09-21).
    # `project_for_namespace` walks binding -> warehouse -> project and does no prefix check, by
    # design — it is a registry read. So a TENANT-LESS lane namespace like `silver`, bound to a
    # warehouse that belongs to a project, resolves to that project. Stamping it there is not a
    # cosmetic error: the medallion's publication head reads this field verbatim and composes the next
    # tier as `project_namespace(project, dataset)`, so the hop targets `<project>-gold$catalog` — a
    # table the runner never wrote — and silver->gold refuses every tabular-lane publication.
    #
    # A CHECK, NOT A PARSE, which is why this is sound where a string rule is not. This module's
    # docstring records that `acme-bronze` cannot be DECOMPOSED into a project, because
    # `PROJECT_PATTERN` permits `-` inside a project id. It can still be verified against a project the
    # registry already answered: `project_namespace` joins with `-` and nothing else, so a qualified
    # namespace always starts with `f"{project}-"`. Nothing here has to choose between `acme` and
    # `acme-eu` — the registry chose, and this only confirms the id agrees.
    #
    # THE LOOKUP ABOVE IS DELIBERATELY UNGATED: `resolve_effective_gate` resolves through the same
    # request-scoped cache, so gating the call rather than the emit would weaken a warehouse's own gate.
    if project and segments[0].startswith(f"{project}-"):
        extra["project"] = project
    return extra


log = logging.getLogger(__name__)


async def _project_from_binding(settings: Settings, so: StorageOptions, top_ns: str) -> str | None:
    """The tenant from the warehouse binding, SWALLOWING failure — the emitter's own contract.

    Swallowing is not defensive padding here, it is the property this fallback has to preserve. The
    emitter states it outright: resolution "cannot raise", so a registry blip costs the watchers their
    tenant and never costs the caller their request. A fallback that raised would turn an unreachable
    or absent registry into a failed publish — which is strictly worse than the defect it was added to
    fix, because it converts a silently weaker gate into a broken door.

    `None` degrades to exactly the pre-existing behaviour: the request's own gate governs.
    """
    try:
        return await run_in_threadpool(warehouses.project_for_namespace, settings.registry_root, so, top_ns)
    except Exception:
        log.debug("gate project resolution failed for %r — the request's own gate governs", top_ns, exc_info=True)
        return None


async def resolve_effective_gate(
    settings: Settings,
    so: StorageOptions,
    lineage: ProjectSource,
    segments: list[str],
    body: PublishRequest,
) -> publication.EffectiveGate:
    """The gate this publish runs under: the project's DECLARED record where one exists, else the request.

    THE DECLARATION IS CONSULTED HERE BECAUSE THIS DOOR IS THE ONE EVERY WRITER SHARES. A writer —
    Spark, an Argo step, a person with credentials — resolves no `GateSpec` for itself, so a door trusting the request
    alone gave the least-trusted writer the weakest gate. The composition rules and why the two fields
    compose differently live on `publication.EffectiveGate`.

    The project comes from the same request-scoped binding `publication_extra` names the tenant with,
    for the same reason: `PROJECT_PATTERN` permits `-` inside a project id, so no string rule recovers
    `acme` from `acme-bronze`, and a declaration is keyed by project. An unresolvable binding leaves
    the request governing — there is no declaration to find without a project id.
    """
    # THE EMITTER FIRST, THE BINDING AS THE FLOOR — and the floor is the fix.
    #
    # This read the emitter alone. With `LANCE_LINEAGE_EMIT_ENABLED=false` that is a `NoopEmitter` whose
    # `project_for` returns None unconditionally, so no project resolved, no declaration loaded, and
    # every project's declared gate silently did not apply while publish still answered 200. Measured by
    # flipping that one variable. Turning off telemetry must not turn off a governance control.
    #
    # The emitter is kept as the fast path rather than replaced, because its resolver is
    # `project_for_namespace` PLUS the request-scoped binding cache (`main.py::_resolve_project`) — so
    # reading the registry directly would answer identically and pay a warehouse read on every publish
    # of a hot namespace. The fallback runs only when the emitter cannot answer, which is exactly the
    # emit-off case and a cold cache.
    project = (await lineage.project_for(segments[0]) if segments else None) or None
    if project is None and segments:
        project = await _project_from_binding(settings, so, segments[0])
    spec = await run_in_threadpool(publication.declared_gate, settings.registry_root, so, project or "")
    return publication.effective_gate(spec, key_column=body.key_column, required_columns=body.required_columns)


@table_router.post("/{id}/publish")
async def publish_table(
    id: str,
    body: PublishRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    lineage: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    fga_client: FgaClientDep,
    token: CurrentToken,
    clock: ClockDep,
    authorization: Annotated[str | None, Header()] = None,
) -> PublishResult:
    """Gate `version` and, if it passes, advance `published` to it; hold it when it breaches under review.

    A refused gate is a 200 with `published=False`, not an error status: the request was well-formed
    and the system did exactly what it should. A held one is a 200 with `held=True` and the hold. Errors
    are reserved for the caller getting it wrong — an unknown version (404), a backwards move (409), a
    malformed one (400), a version older than the one held (409) — all raised as `lance_namespace` typed
    errors so the shared problem-body handler renders them.
    """
    segments = parse_identifier(id)
    if body.accept_assertions:
        # A SECOND door, above the router's `can_update_tag`, and the two rungs are now genuinely
        # different rather than nested. An ordinary publish needs `publisher` — move the ref that says
        # which version is blessed. OVERRIDING a finding the gate raised needs `validator`, the rung
        # the model defines for exactly this ("a plain writer can write within a stage but cannot
        # promote INTO a gated one"). An override must need more permission than an ordinary publish,
        # not the same amount.
        #
        # It used to be nested and therefore weaker: publishing required `owner`, and `validator` is
        # `[...] or owner`, so anyone who could publish at all could already override. The `publisher`
        # rung (owner ruling 2026-09-10) is what makes this second door cost something.
        await require_relation(
            fga_client,
            settings,
            token,
            relation="can_promote",
            obj=fga.table_object(table.resolved.require_uuid()),
        )
    # THE GATE THIS PUBLISH RUNS, composed before either path: the project's DECLARED record where one
    # exists, the request's own values where none does. Resolved here rather than inside `publish` so
    # the gate_only QUESTION and the publish ACT cannot end up under different policy.
    gate = await resolve_effective_gate(settings, so, lineage, segments, body)
    if body.gate_only:
        # A QUESTION: the same assertions on the same version, tag untouched, no hold and no control
        # event. It returns before the publish so nothing here can advance `published` or open a hold by
        # accident — a caller asking "would you accept this?" must never discover it acted as a side effect.
        candidate = await run_in_threadpool(partial(table.open, so, version=body.version))
        published_rows = await run_in_threadpool(_published_rows, table, so) if gate.review is not None else None
        verdict = await run_in_threadpool(
            partial(
                publication.gate,
                candidate.uri,
                key_column=gate.key_column,
                version=body.version,
                required_columns=tuple(gate.required_columns),
                storage_options=so,
                declared_by=gate.declared_by,
                review=gate.review,
                published_rows=published_rows,
            )
        )
        return PublishResult(
            table=verdict.table,
            published=False,
            from_version=verdict.from_version,
            to_version=verdict.to_version,
            assertions=[a.model_dump() for a in verdict.assertions],
            reason=verdict.reason,
            gate_source=verdict.gate_source,
            held=verdict.held,
        )
    result = await run_in_threadpool(
        partial(
            publication.publish,
            table,
            so,
            version=body.version,
            key_column=gate.key_column,
            required_columns=tuple(gate.required_columns),
            accept_assertions=tuple(body.accept_assertions),
            declared_by=gate.declared_by,
            review=gate.review,
        )
    )
    hold: PromotionHold | None = None
    if result.held and gate.review is not None:
        standing = await _hold(
            settings=settings,
            table=table,
            so=so,
            segments=segments,
            table_uuid=await _published_uuid(settings, result, segments),
            result=result,
            review=gate.review,
            requested_by=token.subject if token is not None else None,
            originator=publication_originator(body.originator, token),
            cascade_id=body.cascade_id,
            opened_at=clock(),
        )
        hold = _view(standing, table=fga.canonical_object_id(segments))
        await emit_control(
            control,
            action="promotion_held",
            object_type="table",
            object_id=fga.table_object(standing.uuid),
            object_name=fga.canonical_object_id(segments),
            actor=f"user:{token.subject}" if token is not None else None,
            extra={
                "version": standing.version,
                "published_version": standing.published_version,
                "reasons": standing.reasons,
                "expires_at": standing.expires_at.isoformat(),
            },
        )
    # The NOTIFICATION, and only after the tag actually moved (D-R2). A refused or held gate announces
    # no publication: there is no new readiness to wake anyone for, and an event on a rejection would
    # train consumers to check whether a "published" notice actually published.
    if result.advanced:
        await _announce_publication(
            lineage=lineage,
            uuids=uuids,
            control=control,
            segments=segments,
            table_uuid=await _published_uuid(settings, result, segments),
            token=token,
            authorization=authorization,
            from_version=result.from_version,
            to_version=result.to_version,
            location=result.table,
            accepted=result.accepted,
            cascade_id=body.cascade_id,
            # WHO this publication is FOR, as opposed to `actor`, which is who performed it. They
            # differ on exactly the path that matters: a pipeline's publish is performed by its service
            # principal and is for the person whose bronze write started the batch.
            originator=publication_originator(body.originator, token),
        )
        await _close_what_published_reached(settings=settings, table=table, so=so, location=result.table, published=result.to_version, at=clock())

    return PublishResult(
        table=result.table,
        published=result.published,
        from_version=result.from_version,
        to_version=result.to_version,
        assertions=[a.model_dump() for a in result.assertions],
        reason=result.reason,
        gate_source=result.gate_source,
        held=hold is not None,
        hold=hold,
    )


async def _published_uuid(settings: Settings, result: publication.PublicationResult, segments: list[str]) -> str:
    """The uuid of the table the publish acted on, read off the location it opened, so a rename since cannot pair a
    hold with another table (ADR 0173 §7)."""
    return await run_in_threadpool(table_claims.uuid_at, table_claims.claim_store(settings), result.table, segments)


def _published_rows(table: DoorTable, so: StorageOptions) -> int | None:
    """The row count readers of `published` read now, or `None` when nothing is published."""
    return publication.rows_at(table, so, publication.published_version(table, so))


def _view(hold: promotions.Hold, *, table: str) -> PromotionHold:
    """The hold as the wire shows it, naming its table ``table``: its storage location stays inside the catalog."""
    return PromotionHold.model_validate({**hold.model_dump(exclude={"location", "originator", "cascade_id", "restore_owed"}), "table": table})


async def _hold(
    *,
    settings: Settings,
    table: DoorTable,
    so: StorageOptions,
    segments: list[str],
    table_uuid: str,
    result: publication.PublicationResult,
    review: publication.ReviewPolicy,
    requested_by: str | None,
    originator: str,
    cascade_id: str,
    opened_at: datetime,
) -> promotions.Hold:
    """Record the held version as its table's open hold and pin it; the hold that stands is returned."""
    held = promotions.Hold(
        table=fga.canonical_object_id(segments),
        uuid=table_uuid,
        location=result.table,
        version=result.to_version,
        published_version=result.from_version,
        reasons=result.hold_reasons,
        requested_by=requested_by,
        originator=originator,
        cascade_id=cascade_id,
        opened_at=opened_at,
        expires_at=promotions.deadline(opened_at, review.hours),
    )
    standing = await run_in_threadpool(promotions.open_hold, settings.registry_root, settings.storage_options(), held)
    await run_in_threadpool(promotions.sync_pin, table, so, standing)
    return standing


async def _repin(settings: Settings, table: DoorTable, so: StorageOptions, table_uuid: str) -> None:
    """Point the hold pin at the table's open hold as its record stands now, or remove it when none is open."""
    current = await run_in_threadpool(promotions.hold_of, settings.registry_root, settings.storage_options(), table_uuid)
    await run_in_threadpool(promotions.sync_pin, table, so, current)


async def _close_what_published_reached(*, settings: Settings, table: DoorTable, so: StorageOptions, location: str, published: int, at: datetime) -> None:
    """Close the open hold of the table at ``location`` when `published` moved to or past its version: readers already
    read it.

    Best-effort, because `published` has already moved: a control root or a claim that cannot be read here leaves a
    hold readers have passed, which the decision doors refuse as reached and the sweep expires, and must not turn a
    committed publication into a failed request.
    """
    try:
        table_uuid = await run_in_threadpool(table_claims.uuid_at, table_claims.claim_store(settings), location, table.segments)
        closed = await run_in_threadpool(
            partial(promotions.supersede, settings.registry_root, settings.storage_options(), table_uuid, published=published, at=at)
        )
        if closed is not None:
            await _repin(settings, table, so, table_uuid)
    except (OSError, ValueError, RecordChangedError, InternalError, ConcurrentModificationError):
        log.warning("promotion_supersede_failed", extra={"table": table.segments, "published": published}, exc_info=True)


async def _announce_publication(
    *,
    lineage: LineageEmitter,
    uuids: UuidReader,
    control: ControlEmitter,
    segments: list[str],
    table_uuid: str,
    token: Caller | None,
    authorization: str | None,
    from_version: int | None,
    to_version: int,
    location: str,
    accepted: list[str] | None = None,
    cascade_id: str = "",
    originator: str = "",
) -> None:
    """Announce that `published` moved to `to_version`: a `publish_table` run on lineage, which reaches the
    project's watchers, and `table_published` on the control lane, carrying the range (D-R3).

    `extra` carries the RANGE, which is the whole point of the signal — a consumer resolves {from, to} by the
    change-feed consumer rule (`catalog.services.changes`) and keeps no bookmark. A POLLING consumer that misses
    the event loses nothing, because the tag still answers "what is ready?"; a consumer that acts on the event
    alone does lose it, so the emitter STAGES through the control outbox and a failed publish leaves a
    recoverable object rather than nothing.
    """
    await emit_write_event(
        lineage,
        segments,
        author=token.subject if token is not None else None,
        version=to_version,
        operation=PUBLISH_TABLE,
        authorization=authorization,
        uuids=uuids,
        source_uri=location,
    )
    await emit_control(
        control,
        action="table_published",
        object_type="table",
        object_id=fga.table_object(table_uuid),
        object_name=fga.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        # I2: the caller names {project, dataset}, the CATALOG vends the URI and the tenant.
        # Both were rebuilt downstream before this, and the tenant was rebuilt wrongly.
        extra=await publication_extra(
            lineage,
            segments,
            from_version=from_version,
            to_version=to_version,
            location=location,
            accepted=accepted,
            cascade_id=cascade_id,
            originator=originator,
        ),
    )


async def _decision(
    settings: Settings,
    table: DoorTable,
    so: StorageOptions,
    *,
    version: int,
    state: Literal["approved", "rejected"],
    token: Caller | None,
    at: datetime,
) -> tuple[promotions.Hold, bool]:
    """Record ``state`` on the table's open hold on ``version``: the hold as decided, and whether this call decided it.

    A hold `published` already reached is closed as superseded and refused rather than decided, because its answer
    would act on data readers have moved past: an approval would move the tag backwards, and a rejection would
    restore main past newer published data. The same decision repeated after it took effect answers as decided.

    Raises:
        InvalidTableStateError: No such version is held on the dataset this id opens, or `published` reached it.
    """
    table_uuid = table.resolved.require_uuid()
    location = (await run_in_threadpool(table.open, so)).uri
    previous = await run_in_threadpool(publication.published_version, table, so)
    if previous is not None and previous >= version:
        standing = await run_in_threadpool(promotions.hold_of, settings.registry_root, settings.storage_options(), table_uuid)
        if standing is not None and (standing.version, standing.location, standing.state) == (version, location, state):
            return standing, False
        await _close_what_published_reached(settings=settings, table=table, so=so, location=location, published=previous, at=at)
        raise InvalidTableStateError(f"`published` is at version {previous}, which already carries version {version}")
    return await run_in_threadpool(
        partial(
            promotions.decide,
            settings.registry_root,
            settings.storage_options(),
            table_uuid,
            version=version,
            location=location,
            state=state,
            by=token.subject if token is not None else None,
            at=at,
            restore_owed=state == "rejected" and previous is not None,
        )
    )


@table_router.post("/{id}/promotion/approve")
async def approve_promotion(
    id: str,
    body: PromotionDecisionRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    lineage: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    token: CurrentToken,
    clock: ClockDep,
    authorization: Annotated[str | None, Header()] = None,
) -> PromotionDecision:
    """Approve the held `version`: `published` moves to it (the spec's `UpdateTableTag`) and its watchers are told.

    Gated on `can_promote` by the router (`fga_deps`), and the tier judge decides it like every write. The approval
    is recorded before the tag moves, so approving again completes one whose tag move failed and otherwise answers
    as decided. 409 when the table holds no such version, it was decided otherwise, or `published` reached it.
    """
    segments = parse_identifier(id)
    table_uuid = table.resolved.require_uuid()
    decided, _ = await _decision(settings, table, so, version=body.version, state="approved", token=token, at=clock())
    moved, previous = await run_in_threadpool(publication.advance, table, so, decided.version)
    if moved:
        await _announce_publication(
            lineage=lineage,
            uuids=uuids,
            control=control,
            segments=segments,
            table_uuid=table_uuid,
            token=token,
            authorization=authorization,
            from_version=previous,
            to_version=decided.version,
            location=decided.location,
            cascade_id=decided.cascade_id,
            originator=decided.originator,
        )
    await _repin(settings, table, so, table_uuid)
    canonical = fga.canonical_object_id(segments)
    return PromotionDecision(hold=_view(decided, table=canonical), published_version=decided.version if moved else previous)


@table_router.post("/{id}/promotion/reject")
async def reject_promotion(
    id: str,
    body: PromotionDecisionRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    lineage: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    token: CurrentToken,
    clock: ClockDep,
    authorization: Annotated[str | None, Header()] = None,
) -> PromotionDecision:
    """Reject the held `version`: main is restored to the published version (the spec's `RestoreTable`), as a new
    commit, so the next writer builds on what readers read. `published` does not move.

    Gated on `can_promote` by the router (`fga_deps`), and the tier judge decides it like every write. The rejection
    is recorded before main is restored, and main is restored once: rejecting again completes a restore the first
    call could not make, and otherwise answers as decided. With nothing published, or `published` moved to or past
    the held version meanwhile, there is nothing to restore and the rejection only closes the hold. 409 when the
    table holds no such version, it was decided otherwise, or `published` reached it.
    """
    segments = parse_identifier(id)
    table_uuid = table.resolved.require_uuid()
    canonical = fga.canonical_object_id(segments)
    decided, claimed = await _decision(settings, table, so, version=body.version, state="rejected", token=token, at=clock())
    if decided.restore_owed:
        published = await run_in_threadpool(publication.published_version, table, so)
        restored: int | None = None
        if published is not None and published < decided.version:
            restore = partial(
                dataplane.restore,
                table,
                so,
                RestoreTableRequest(id=segments, version=published),
                transaction_properties=lineage_deps.door_stamp(token, RESTORE_TABLE),
            )
            _, restored = await run_in_threadpool(restore)
            await lineage_deps.emit_measured_write(
                lineage,
                segments,
                location=table.location,
                so=so,
                token=token,
                operation=RESTORE_TABLE,
                authorization=authorization,
                uuids=uuids,
                pin_version=restored,
                branch=None,
            )
        decided = await run_in_threadpool(
            partial(promotions.record_restore, settings.registry_root, settings.storage_options(), table_uuid, version=decided.version, restored=restored)
        )
    await _repin(settings, table, so, table_uuid)
    published = await run_in_threadpool(publication.published_version, table, so)
    if claimed:
        await emit_control(
            control,
            action="promotion_rejected",
            object_type="table",
            object_id=fga.table_object(table_uuid),
            object_name=canonical,
            actor=f"user:{token.subject}" if token is not None else None,
            extra={"version": decided.version, "published_version": published, "restored_version": decided.restored_version},
        )
    return PromotionDecision(hold=_view(decided, table=canonical), published_version=published, restored_version=decided.restored_version)


@promotions_router.get("")
async def list_promotions(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    page_token: Annotated[str | None, Query(description="Continue after this table id (the previous page's `page_token`).")] = None,
) -> PromotionsListResponse:
    """The open holds the caller may see, in table order: each table's held version, what `published` names, why
    it is held and when the sweep expires it. A hold names its table as the table is named now, and is listed to a
    caller holding `can_get_metadata` on it; a hold whose table is gone is listed to nobody, and expires.
    """
    holds = await run_in_threadpool(promotions.open_holds, settings.registry_root, settings.storage_options())
    entries = [table_claims.TableRecord(location=hold.location, uuid=hold.uuid) for hold in holds]
    names = await run_in_threadpool(table_claims.names_now, table_claims.claim_store(settings), entries)
    named = [(name, hold) for name, hold in zip(names, holds, strict=True) if name is not None]
    if (authz := authorization(settings, client, token)) is not None:
        # Checked on the uuid each hold records ([[LH-524]]), which is the object the table's grants are held by.
        visible = await authz.batch_check("can_get_metadata", sorted({fga.table_object(hold.uuid) for _, hold in named}))
        named = [(name, hold) for name, hold in named if visible.get(fga.table_object(hold.uuid))]
    by_key = {f"{name}|{hold.uuid}": (name, hold) for name, hold in named}
    page, next_token = paginate(sorted(by_key), page_token, limit)
    return PromotionsListResponse(holds=[_view(hold, table=name) for name, hold in (by_key[key] for key in page)], page_token=next_token)


# One exported router, like `gates`: `router.py` mounts `_module.router` for every endpoint module.
router = APIRouter()
router.include_router(table_router)
router.include_router(promotions_router)
