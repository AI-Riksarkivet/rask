"""Governed business-metadata endpoints (#49): human-curated dataset tags + description.

The read (``GET /datasets/{name}/governance``) is reader-gated like every other per-``{name}`` dataset
read; the writes (``PUT``/``DELETE`` tag, ``PUT`` description) are gated on the writer rung
(``can_write_data`` — the same permission :func:`~lineage.api.fga_deps.enforce_output_authz` demands of a
producer recording provenance for that dataset). Every change is attributable: the caller's verified
subject + a UTC timestamp are persisted on the node (last-writer per field family) and the repository
logs a structured line. Tags share the node property the OpenLineage ``tags`` facet populates — ingest
UNIONs, so producer-asserted and human tags coexist; removing a producer-asserted tag lasts until that
producer's next tagged run re-asserts it.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from lance_namespace import InvalidInputError, TableNotFoundError

from lineage.api.dependencies import RepositoryDep
from lineage.api.fga_deps import audit_read, require_metadata_access, require_write_access
from lineage.api.security import CurrentToken
from lineage.schemas import DatasetGovernance, DescriptionUpdate


# A governance tag is a short label, optionally ``key=value``. The comma is the node property's join
# separator so it can never appear in a tag, and ``/`` is excluded too — the tag rides in a URL path
# segment, and %2F handling differs across routers/proxies, so allowing it invites route ambiguity.
# The rest mirrors the shapes producers already emit (``layer=bronze``); fullmatch anchors both ends.
# The regex is the SINGLE validation source (a Path length constraint beside it would answer the same
# input 422 at the transport while this answers 400 — one mechanism, one status).
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._=-]{0,63}")

TagParam = Annotated[str, Path()]

read_router = APIRouter(
    prefix="/datasets",
    tags=["governance"],
    dependencies=[Depends(require_metadata_access), Depends(audit_read)],
)
write_router = APIRouter(
    prefix="/datasets",
    tags=["governance"],
    dependencies=[Depends(require_write_access)],
)


def _validated(tag: str) -> str:
    if not _TAG_RE.fullmatch(tag):
        raise InvalidInputError(f"invalid tag {tag!r}: [A-Za-z0-9._=-], starting alphanumeric, max 64 chars")
    return tag


def _subject(token: CurrentToken) -> str:
    """The attributable subject: the verified principal, or ``anonymous`` in the open dev mode."""
    return token.sub if token is not None else "anonymous"


def _found(governance: DatasetGovernance | None, name: str) -> DatasetGovernance:
    if governance is None:
        raise TableNotFoundError(f"unknown dataset: {name}")
    return governance


@read_router.get("/{name}/governance")
async def get_governance(name: str, repository: RepositoryDep) -> DatasetGovernance:
    """The dataset's curated tags + description with last-writer attribution (who/when per field)."""
    return _found(await repository.governance(name), name)


@write_router.put("/{name}/tags/{tag}")
async def add_tag(name: str, tag: TagParam, repository: RepositoryDep, token: CurrentToken) -> DatasetGovernance:
    """Add one governance tag (idempotent — re-adding an existing tag is a no-op re-stamp)."""
    updated = await repository.set_tag(name, _validated(tag), present=True, updated_by=_subject(token))
    return _found(updated, name)


@write_router.delete("/{name}/tags/{tag}")
async def remove_tag(name: str, tag: TagParam, repository: RepositoryDep, token: CurrentToken) -> DatasetGovernance:
    """Remove one governance tag (idempotent — removing an absent tag is a no-op re-stamp).

    Deliberately unvalidated: removal only filters an existing element out of storage, so any string is
    safe — and it lets a curator purge a non-conforming tag (e.g. an old producer label) that the add
    path would refuse."""
    updated = await repository.set_tag(name, tag, present=False, updated_by=_subject(token))
    return _found(updated, name)


@write_router.put("/{name}/description")
async def set_description(name: str, body: DescriptionUpdate, repository: RepositoryDep, token: CurrentToken) -> DatasetGovernance:
    """Set the dataset's description (an empty string clears it)."""
    updated = await repository.set_description(name, body.description, updated_by=_subject(token))
    return _found(updated, name)


# The v1 aggregator includes one ``router`` per module — the two rungs stay separate routers (reader vs
# writer gate as router-level deps) and are stitched here.
router = APIRouter()
router.include_router(read_router)
router.include_router(write_router)
