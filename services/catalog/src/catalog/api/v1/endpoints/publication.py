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

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from catalog.api.dependencies import NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.core.identifiers import parse_identifier
from catalog.schemas import PublishRequest, PublishResult
from catalog.services import publication


router = APIRouter(prefix="/v1/table", tags=["publication"])


@router.post("/{id}/publish")
async def publish_table(
    id: str,
    body: PublishRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
) -> PublishResult:
    """Gate `version` and, if it passes, advance `published` to it.

    A refused gate is a 200 with `published=False`, not an error status: the request was well-formed
    and the system did exactly what it should. Errors are reserved for the caller getting it wrong —
    an unknown version (404), a backwards move (409), a malformed one (400) — all raised as
    `lance_namespace` typed errors so the shared problem-body handler renders them.
    """
    segments = parse_identifier(id, settings.delimiter)
    result = await run_in_threadpool(
        publication.publish,
        ns,
        so,
        table_id=segments,
        version=body.version,
        key_column=body.key_column,
        required_columns=tuple(body.required_columns),
    )
    return PublishResult(
        table=result.table,
        published=result.published,
        from_version=result.from_version,
        to_version=result.to_version,
        assertions=[a.model_dump() for a in result.assertions],
        reason=result.reason,
    )
