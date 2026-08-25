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
from catalog.core.identifiers import parse_identifier
from catalog.core.namespace import open_dataset
from catalog.schemas import PublishRequest, PublishResult
from catalog.services import publication
from service_kit.control_emit import emit_control
from service_kit.governed import fga


router = APIRouter(prefix="/v1/table", tags=["publication"])


class ProjectSource(Protocol):
    """The one method `publication_extra` needs. `LineageEmitter` satisfies it and carries the
    request-scoped binding cache; demanding the whole emitter would ask for six more it does not use."""

    async def project_for(self, top_ns: str) -> str | None: ...


#: The lineage emitter, narrowed to the one method this route uses. Same instance, same cache.
ProjectSourceDep = Annotated[ProjectSource, Depends(get_lineage_emitter)]


async def publication_extra(
    lineage: ProjectSource,
    segments: list[str],
    *,
    from_version: int | None,
    to_version: int | None,
    location: str,
    accepted: list[str] | None = None,
    cascade_id: str = "",
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
    project = await lineage.project_for(segments[0]) if segments else None
    if project:
        extra["project"] = project
    return extra


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
    if body.gate_only:
        # A QUESTION: the same assertions on the same version, tag untouched, no control event. It
        # returns before the publish so nothing here can advance `published` by accident — a caller
        # asking "would you accept this?" must never discover it published as a side effect.
        candidate = await run_in_threadpool(partial(open_dataset, ns, so, segments, version=body.version))
        verdict = await run_in_threadpool(
            publication.gate,
            candidate.uri,
            key_column=body.key_column,
            version=body.version,
            required_columns=tuple(body.required_columns),
            storage_options=so,
        )
        return PublishResult(
            table=verdict.table,
            published=False,
            from_version=verdict.from_version,
            to_version=verdict.to_version,
            assertions=[a.model_dump() for a in verdict.assertions],
            reason=verdict.reason,
        )
    result = await run_in_threadpool(
        publication.publish,
        ns,
        so,
        table_id=segments,
        version=body.version,
        key_column=body.key_column,
        required_columns=tuple(body.required_columns),
        accept_assertions=tuple(body.accept_assertions),
    )
    # The NOTIFICATION, and only after the tag actually moved (D-R2). A refused gate announces
    # nothing: there is no new readiness to wake anyone for, and an event on a rejection would train
    # consumers to check whether a "published" notice actually published.
    #
    # `extra` carries the RANGE, which is the whole point of the signal (D-R3) — a consumer turns
    # {from, to} straight into `_row_created_at_version > from AND <= to` and keeps no bookmark. A
    # consumer that MISSES this event loses nothing: the `published` tag still answers "what is
    # ready?", which is why the tag is the truth and this is merely the wake-up.
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
            ),
        )

    return PublishResult(
        table=result.table,
        published=result.published,
        from_version=result.from_version,
        to_version=result.to_version,
        assertions=[a.model_dump() for a in result.assertions],
        reason=result.reason,
    )
