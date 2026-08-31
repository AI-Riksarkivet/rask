"""`POST /v1/table/{id}/publish` — the one door through which data becomes consumable.

`open_ingest.md` § D2 (D-R1/D-R2/D-R3). A commit makes a version READABLE; publishing makes it
READY. The gate runs against the committed version and only a pass advances the `published` tag, so
a consumer reading through the pointer never sees a batch that failed.

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

from functools import partial
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import (
    ControlEmitterDep,
    FgaClientDep,
    NamespaceDep,
    SettingsDep,
    StorageOptionsDep,
    get_lineage_emitter,
)
from catalog.api.fga_deps import require_relation
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier
from catalog.core.lineage_emit import is_person_subject
from catalog.core.namespace import open_dataset
from catalog.schemas import PublishRequest, PublishResult
from catalog.services import publication, warehouses
from service_kit.control_emit import emit_control
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


router = APIRouter(prefix="/v1/table", tags=["publication"])


class ProjectSource(Protocol):
    """The one method `publication_extra` needs. `LineageEmitter` satisfies it and carries the
    request-scoped binding cache; demanding the whole emitter would ask for six more it does not use."""

    async def project_for(self, top_ns: str) -> str | None: ...


#: The lineage emitter, narrowed to the one method this route uses. Same instance, same cache.
ProjectSourceDep = Annotated[ProjectSource, Depends(get_lineage_emitter)]


#: NO LOCAL COPY OF "is this a person" LIVES HERE. `catalog.core.lineage_emit.is_person_subject` is the
#: one definition, and its own docstring says why: "the two disagreeing is the whole failure mode".
#: This module briefly declared a weakened set — `{"", "*", "user:*"}` — which omitted the ROLE
#: LITERALS the canonical set carries (`ray`, `data_eng`, `analyst`, `anon`, `system`, `service`).
#: Measured on that version, `publication_originator("data_eng", …)` returned `"data_eng"`, so a failed
#: gold stage still wrote a row into an inbox actor named after a role — verbatim the symptom this
#: door was added to eliminate (`.claude/skills/rask-notifications`, trap 1).


def publication_originator(claimed: str, token: IDToken | None) -> str:
    """The PERSON this publication is for, or `""` when it is for nobody.

    THE CATALOG IS THE ONLY COMPONENT THAT CAN ANSWER THIS, which is why the decision lives here
    rather than at the consumer. `table_published` is what wakes the next cascade hop, and the hop
    needs to know whether the caller was a person or a service — a question only the door that
    authenticated them can answer. `IDToken.service` is set by `catalog/api/security.py` when a caller
    comes through the SERVICE door, and this is its first reader.

    Precedence, and both halves are load-bearing:

    * A SERVICE caller's `claimed` wins. A mover publishes as `service-<mover>`, so the actor is a
      role, not an address; the human is only on the request body, carried there from the cascade head
      by `catalog_register.publish_stage_output`.
    * A PERSON's own sub wins over anything they claimed. Someone publishing by hand IS the
      originator, and honouring a body field over a verified sub would let a caller redirect a
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
    # `service` is an EXTRA claim (`IDToken` is `extra="allow"`), stamped only by the service door, so
    # it is read by name rather than declared — a real IdP token carries no such field and a caller
    # cannot add one, because this object is built from verified claims, never from the request body.
    if not getattr(token, "service", False):
        return _addressable(token.sub)
    return _addressable(claimed)


def _addressable(subject: str) -> str:
    """`subject` if it names a person, else `""` — the `named_subject` rule the control lane applies.

    The optional `user:` prefix is STRIPPED rather than refused, because both spellings reach this
    field honestly: the medallion carries a bare sub (what `authorize_produce` returns) while an FGA
    principal is written `user:<sub>`. Refusing one of them would drop a real person for a wire-shape
    difference, silently and with a 200.
    """
    # STRIP BEFORE CHECKING. `is_person_subject` treats a `user:`-prefixed value as an FGA object id
    # rather than a subject, so checking first would refuse `user:alice` — a real person, written in
    # the spelling an FGA principal arrives in. Strip, then let the canonical check judge the bare sub.
    candidate = subject.strip().removeprefix("user:").strip()
    return "" if not is_person_subject(candidate) or "#" in candidate else candidate


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
    if project:
        extra["project"] = project
    return extra


async def resolve_effective_gate(
    settings: Settings,
    so: dict[str, str],
    lineage: ProjectSource,
    segments: list[str],
    body: PublishRequest,
) -> publication.EffectiveGate:
    """The gate this publish runs under: the project's DECLARED record where one exists, else the request.

    THE DECLARATION IS CONSULTED HERE BECAUSE THIS DOOR IS THE ONE EVERY WRITER SHARES. A mover
    resolves the same `GateSpec` for itself before it ever calls the catalog; an external writer —
    Spark, an Argo step, a person with credentials — resolves nothing, so a door trusting the request
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
        project = await run_in_threadpool(warehouses.project_for_namespace, settings.registry_root, so, segments[0])
    spec = await run_in_threadpool(publication.declared_gate, settings.registry_root, so, project or "")
    return publication.effective_gate(spec, key_column=body.key_column, required_columns=body.required_columns)


@router.post("/{id}/publish")
async def publish_table(
    id: str,
    body: PublishRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    lineage: ProjectSourceDep,
    fga_client: FgaClientDep,
    token: CurrentToken,
) -> PublishResult:
    """Gate `version` and, if it passes, advance `published` to it.

    A refused gate is a 200 with `published=False`, not an error status: the request was well-formed
    and the system did exactly what it should. Errors are reserved for the caller getting it wrong —
    an unknown version (404), a backwards move (409), a malformed one (400) — all raised as
    `lance_namespace` typed errors so the shared problem-body handler renders them.
    """
    segments = parse_identifier(id, settings.delimiter)
    if body.accept_assertions:
        # A SECOND door, above the router's `can_update_tag`. Publishing is an owner-tier act;
        # accepting a finding the gate raised is a VALIDATOR's — the rung the model defines for
        # exactly this ("a plain writer can write within a stage but cannot promote INTO a gated
        # one"). An override must therefore need more permission than an ordinary publish, not the
        # same amount.
        await require_relation(
            fga_client,
            settings,
            token,
            relation="can_promote",
            obj=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        )
    # THE GATE THIS PUBLISH RUNS, composed before either path: the project's DECLARED record where one
    # exists, the request's own values where none does. Resolved here rather than inside `publish` so
    # the gate_only QUESTION and the publish ACT cannot end up under different policy.
    gate = await resolve_effective_gate(settings, so, lineage, segments, body)
    if body.gate_only:
        # A QUESTION: the same assertions on the same version, tag untouched, no control event. It
        # returns before the publish so nothing here can advance `published` by accident — a caller
        # asking "would you accept this?" must never discover it published as a side effect.
        candidate = await run_in_threadpool(partial(open_dataset, ns, so, segments, version=body.version))
        verdict = await run_in_threadpool(
            publication.gate,
            candidate.uri,
            key_column=gate.key_column,
            version=body.version,
            required_columns=tuple(gate.required_columns),
            storage_options=so,
            declared_by=gate.declared_by,
        )
        return PublishResult(
            table=verdict.table,
            published=False,
            from_version=verdict.from_version,
            to_version=verdict.to_version,
            assertions=[a.model_dump() for a in verdict.assertions],
            reason=verdict.reason,
            gate_source=verdict.gate_source,
        )
    result = await run_in_threadpool(
        publication.publish,
        ns,
        so,
        table_id=segments,
        version=body.version,
        key_column=gate.key_column,
        required_columns=tuple(gate.required_columns),
        accept_assertions=tuple(body.accept_assertions),
        declared_by=gate.declared_by,
    )
    # The NOTIFICATION, and only after the tag actually moved (D-R2). A refused gate announces
    # nothing: there is no new readiness to wake anyone for, and an event on a rejection would train
    # consumers to check whether a "published" notice actually published.
    #
    # `extra` carries the RANGE, which is the whole point of the signal (D-R3) — a consumer turns
    # {from, to} straight into `_row_created_at_version > from AND <= to` and keeps no bookmark.
    #
    # THIS COMMENT USED TO SAY a consumer that misses the event "loses nothing", because the tag still
    # answers "what is ready?". That is true of a POLLING consumer and false of the one that matters:
    # under `medallion.cascadeViaPublish` the mover deliberately does not fire its own topic, so the
    # silver->gold hop happens ONLY when `/publication-arrival` receives this event — and the
    # medallion plane runs no cron and no reconcile binding, so it never re-reads the tag. A dropped
    # event there cancels the cascade outright, with every pod green.
    #
    # The emitter now STAGES through the control outbox, so a failed publish leaves a recoverable
    # object rather than nothing, and `cascadeViaPublish` refuses to start without one configured.
    if result.advanced:
        await emit_control(
            control,
            action="table_published",
            object_type="table",
            object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
            actor=f"user:{token.sub}" if token is not None else None,
            # I2: the caller names {project, dataset}, the CATALOG vends the URI and the tenant.
            # Both were rebuilt downstream before this, and the tenant was rebuilt wrongly.
            extra=await publication_extra(
                lineage,
                segments,
                from_version=result.from_version,
                to_version=result.to_version,
                location=result.table,
                accepted=result.accepted,
                cascade_id=body.cascade_id,
                # WHO this publication is FOR, as opposed to `actor`, which is who performed it. They
                # differ on exactly the path that matters: a cascade publish is performed by a mover
                # and is for the person whose `/produce` started the batch.
                originator=publication_originator(body.originator, token),
            ),
        )

    return PublishResult(
        table=result.table,
        published=result.published,
        from_version=result.from_version,
        to_version=result.to_version,
        assertions=[a.model_dump() for a in result.assertions],
        reason=result.reason,
        gate_source=result.gate_source,
    )
