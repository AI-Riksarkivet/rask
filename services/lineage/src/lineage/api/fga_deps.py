"""In-service authz for the lineage read + ingest endpoints.

The lineage service owns the audit graph, so it must protect it itself (in-service, not
via a gateway). It mirrors the catalog's authz guard (``services/catalog/api/fga_deps.py``) and **reuses
the catalog's core** — :func:`common.fga.check` / ``batch_check`` — so the OpenFGA check has
one source of truth. The thin FastAPI authz + filter dependencies are re-derived here
because they bind to ``LineageSettings`` rather than the catalog's ``Settings``. (Shared
*library* code; the service makes no runtime call to the catalog — it talks only to the IdP
and the shared OpenFGA store, read-only.)

Three holes this closes (audit ``w8u4rc2tg``):

* **Reads** (``upstream``/``downstream``/``producers``/``graph``) leaked the entire data
  estate. Each is now gated on OpenFGA ``can_get_metadata`` of ``table:<dataset>`` — the
  same permission the catalog requires to ``describe`` that table.
* **Transitive disclosure.** A neighbor/graph read also returns *related* dataset names, so
  :class:`DatasetFilter` (and :func:`governed`) batch-check each and drop the ones the caller
  may not see — mirroring the catalog's ``list_objects``-filtered enumerations.
* **Ingest** was unauthenticated and the run ``author`` was a producer-supplied facet, so
  provenance was forgeable. The author is taken from the verified token
  (:func:`enforce_author`) — the client-claimed facet is overwritten.

Default OFF (``LINEAGE_FGA_ENABLED``), exactly like the catalog; production enables it.
Fail-closed when enabled-but-unwired (503, never silent allow).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

from common import fga
from fastapi import Depends, Request
from lance_namespace import (
    PermissionDeniedError,
    ServiceUnavailableError,
    UnauthenticatedError,
)

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.security import CurrentToken, Principal
from lineage.core.config import LineageSettings
from lineage.models import RunEvent


log = logging.getLogger(__name__)


async def _require_relation(relation: str, name: str, request: Request, settings: LineageSettings, token: Principal | None) -> None:
    """The one fail-closed authz ladder every per-``{name}`` gate shares (audit 2026-07-16: it had
    grown four near-copies). No-op when FGA is off; unwired client → 503; unauthenticated → 401;
    deny → 403; an OpenFGA outage inside ``fga.check`` → 503, never allow."""
    if not settings.fga_enabled:
        return
    client = getattr(request.app.state, "fga", None)
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    obj = f"{settings.fga_object_type}:{name}"
    if not await fga.check(client, user=token.sub, relation=relation, obj=obj):
        log.info("access_denied", extra={"sub": token.sub, "relation": relation, "object": obj})
        raise PermissionDeniedError(f"{relation} required on {obj}")


async def require_metadata_access(name: str, request: Request, settings: SettingsDep, token: CurrentToken) -> None:
    """Gate a dataset read on OpenFGA ``can_get_metadata`` for ``<type>:<name>`` — the same metadata-read
    permission the catalog requires to describe that table."""
    await _require_relation("can_get_metadata", name, request, settings, token)


async def require_write_access(name: str, request: Request, settings: SettingsDep, token: CurrentToken) -> None:
    """Gate a governance write (tags/description, #49) on OpenFGA ``can_write_data`` for ``<type>:<name>``
    — the same writer rung :func:`enforce_output_authz` requires of a producer recording provenance:
    curating a dataset's governance metadata is a write on that dataset."""
    await _require_relation("can_write_data", name, request, settings, token)


async def audit_read(name: str, settings: SettingsDep, token: CurrentToken, repository: RepositoryDep) -> None:
    """Record a read-audit row (WHO read this dataset) on a gated read — best-effort, off by default (#6).

    Complements the write provenance in the AGE graph with an access log. No-op when ``read_audit_enabled``
    is off or the request is unauthenticated (no subject to attribute). An audit-write failure is logged,
    never raised — auditing must never break a read. Runs AFTER :func:`require_metadata_access`, so only an
    authorized read is logged.
    """
    if not settings.read_audit_enabled or token is None:
        return
    try:
        await repository.record_read(reader=token.sub, dataset=name)
    except Exception as exc:
        log.warning("read_audit_failed", extra={"reader": token.sub, "dataset": name, "error": str(exc)})


def enforce_author(event: RunEvent, token: Principal | None) -> None:
    """Bind the run author to the *verified* principal — never trust the request body.

    When the request is authenticated, overwrite the ``author`` run facet with the token
    subject so a producer cannot self-assert someone else's identity (provenance forgery).
    When OIDC is off (dev/tests) the body-supplied author is left as-is.
    """
    if token is not None:
        event.run.facets["author"] = {"name": token.sub, "sub": token.sub}


async def enforce_output_authz(event: RunEvent, request: Request, settings: LineageSettings, token: Principal | None) -> None:
    """Output-scoped ingest authz: require the producer may WRITE every output dataset it claims.

    :func:`enforce_author` proves WHO is ingesting; this proves they were AUTHORIZED to write those outputs —
    a producer cannot record provenance for a table it has no ``can_write_data`` on (the same write
    permission the catalog requires to mutate that table). No-op when FGA is off. Fail-closed BEFORE any
    empty-set short-circuit: unwired client → 503, unauthenticated → 401, any non-writable output → 403.
    Inputs are also authorized (``can_get_metadata``): you may only record READING a dataset you can see,
    so an authenticated reader can't forge READ-edge provenance for datasets outside its reach.
    """
    if not settings.fga_enabled:
        return
    # Fail-closed FIRST — BEFORE any empty-set short-circuit: an authenticated but unauthorized caller must
    # not be able to ingest a run (forging graph state) merely by declaring no outputs. (bug hunt 2026-07-13)
    client = getattr(request.app.state, "fga", None)
    if client is None:
        raise ServiceUnavailableError("authorization service is not available")
    if token is None:
        raise UnauthenticatedError("authentication required")
    object_type = settings.fga_object_type
    outputs = [d.name for d in event.outputs if d.name]
    if outputs:
        allowed = await fga.batch_check(client, user=token.sub, relation="can_write_data", objects=[f"{object_type}:{n}" for n in outputs])
        denied = sorted(n for n in outputs if not allowed.get(f"{object_type}:{n}"))
        if denied:
            log.info("ingest_denied", extra={"sub": token.sub, "relation": "can_write_data", "outputs": denied})
            raise PermissionDeniedError(f"can_write_data required on outputs: {', '.join(denied)}")
    # Inputs: you may only RECORD reading a dataset you can SEE — else an authenticated reader (e.g. the
    # service-web read identity) could forge READ-edge provenance like "service-web read gold$catalog" into
    # the governed audit graph. `writer ⊇ reader` in model.fga, so movers (writers) and the trainer (reader)
    # still pass; only a claim to have read an unreachable dataset is refused. (bug hunt 2026-07-13)
    inputs = [d.name for d in event.inputs if d.name]
    if inputs:
        objs = [f"{object_type}:{n}" for n in inputs]
        seen = await fga.batch_check(client, user=token.sub, relation="can_get_metadata", objects=objs)
        hidden = sorted(n for n in inputs if not seen.get(f"{object_type}:{n}"))
        if hidden:
            log.info(
                "ingest_input_denied",
                extra={"sub": token.sub, "relation": "can_get_metadata", "inputs": hidden},
            )
            raise PermissionDeniedError(f"can_get_metadata required on inputs: {', '.join(hidden)}")


class DatasetFilter:
    """Drop datasets the caller may not see from a lineage result (fail-closed).

    A neighbor/graph read returns *related* dataset names beyond the requested one; without
    filtering, one table grant would disclose the existence of every table in its lineage
    neighborhood. This batch-checks ``can_get_metadata`` (fail-closed: an OpenFGA outage →
    503) and returns only the authorized names — the lineage analogue of the catalog's
    ``list_objects``-filtered enumerations (``services/catalog/api/v1/endpoints/tables.py``). Pass-through
    when FGA is off (dev/tests).
    """

    def __init__(self, request: Request, settings: LineageSettings, token: Principal | None) -> None:
        self._request = request
        self._settings = settings
        self._token = token

    async def visible(self, names: list[str]) -> set[str]:
        """Return the subset of ``names`` the caller may read (``can_get_metadata``)."""
        if not self._settings.fga_enabled or not names:
            return set(names)
        client = getattr(self._request.app.state, "fga", None)
        if client is None:
            raise ServiceUnavailableError("authorization service is not available")
        if self._token is None:
            raise UnauthenticatedError("authentication required")
        object_type = self._settings.fga_object_type
        allowed = await fga.batch_check(
            client,
            user=self._token.sub,
            relation="can_get_metadata",
            objects=[f"{object_type}:{n}" for n in names],
        )
        return {n for n in names if allowed.get(f"{object_type}:{n}")}


def get_dataset_filter(request: Request, settings: SettingsDep, token: CurrentToken) -> DatasetFilter:
    """Build the per-request dataset-visibility filter."""
    return DatasetFilter(request, settings, token)


FilterDep = Annotated[DatasetFilter, Depends(get_dataset_filter)]


async def governed[T](
    datasets: DatasetFilter,
    fga_enabled: bool,
    items: list[T],
    refs: Callable[[T], set[str]],
) -> list[T]:
    """Drop items the caller may not see: any referencing a non-visible dataset, and — when FGA is on —
    any dataset-less item (it would otherwise pass vacuously, leaking run/author/error to a caller with
    no grants). Auth off → ``visible`` is pass-through, so nothing is dropped. (#22 audit)
    """
    referenced = {name for item in items for name in refs(item)}
    visible = await datasets.visible(list(referenced))
    kept: list[Any] = []
    for item in items:
        names = refs(item)
        if fga_enabled and not names:
            continue
        if names <= visible:
            kept.append(item)
    return kept
