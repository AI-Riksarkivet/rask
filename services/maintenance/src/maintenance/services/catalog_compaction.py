"""The catalog's two metadata halves of a compaction, over HTTP — the planner and the committer.

docs/DECISIONS.md "Maintenance leaves the planner pod". `compaction_executor` deliberately takes these as CALLABLES and
constructs no client: what it owns is the execute phase and the ordering, and keeping the transport
out of it is what makes M3 — submitting the same tasks as a `RayJob` — a change of one callable
rather than a rewrite.

Shaped like `credentials.py`, which is the estate's other maintenance→catalog client, and for the
same reasons: both halves of the identity (the Dapr app token AND the claimed subject) or neither,
`params` rather than a body where the door declares a query parameter, and a narrow `except` so a
`NameError` in this module cannot be reported as "the catalog is unavailable".

The two calls are NOT symmetric in how they fail, and that asymmetry is deliberate:

* a PLAN that does not answer raises :class:`CompactionPlaneUnavailable`, and the caller may fall back
  to the in-pod rewrite — nothing was planned, so nothing was written. A plan the door REFUSES as
  malformed raises :class:`CompactionPlanRefused`, which permits no fallback: the request is ours. A
  404 naming the table or namespace absent raises :class:`TableNotGoverned` (:func:`table_not_governed`,
  which the vend client reads the same way): the id is what is wrong;
* a COMMIT that does not answer RAISES. The bytes are already written; reporting success would leave
  a table that looks compacted, is not, and has orphans nobody will attribute.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from lance_namespace.errors import ErrorCode

from maintenance.services.compaction_executor import (
    CommittedWork,
    CompactionPlaneUnavailable,
    CompactionPlanRefused,
    DistributedCompactionError,
    MaintenanceDenied,
    MaintenanceUnauthenticated,
    PlannedWork,
    TableNotGoverned,
    denial_remedy,
    unauthenticated_remedy,
)


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings
from maintenance.services.catalog_identity import service_headers


log = logging.getLogger(__name__)

#: The plan is a manifest read and the commit is one metadata transaction — neither is unbounded in
#: the data, so a call that blocks longer than this is a door in trouble rather than a big table.
_TIMEOUT_SECONDS = 30.0

#: The 4xx answers that ask the caller to retry (RFC 9110 408, RFC 6585 429) rather than saying
#: anything about the request, so they stay an unavailable plane.
_RETRY_LATER = frozenset({408, 429})

#: The 404 codes that answer about the TABLE rather than the request: spec.yaml `ErrorResponse.code`
#: 1 and 4 name an absence, where 13 is the malformed request. Any other 404 is the executor's to fix:
#: the catalog app answers a path no door serves with code 0.
_ABSENT = frozenset({ErrorCode.NAMESPACE_NOT_FOUND, ErrorCode.TABLE_NOT_FOUND})


def _problem(response: httpx.Response) -> tuple[int | None, str]:
    """The door's RFC 9457 lance-namespace ``code`` and ``detail``; the head of the body stands in for a missing detail."""
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:200]
    if not isinstance(body, dict):
        return None, response.text[:200]
    code, detail = body.get("code"), body.get("detail")
    return (code if isinstance(code, int) else None), (str(detail) if detail is not None else response.text[:200])


def table_not_governed(response: httpx.Response, *, table_id: str) -> TableNotGoverned | None:
    """`TableNotGoverned` when ``response`` is a 404 naming the table or namespace absent; ``None`` for any other answer.

    The one reading of "the catalog has no table by this id", shared by the plan and vend clients so the two
    doors cannot disagree about it. A 404 carrying any other code is not this answer.
    """
    if response.status_code != 404:
        return None
    code, detail = _problem(response)
    if code is None or code not in _ABSENT:
        return None
    return TableNotGoverned(f"the catalog answered 404 {ErrorCode(code).name} (code {code}) for {table_id}: {detail}")


def plan_via_catalog(table_id: str, policy: dict[str, Any], *, settings: MaintenanceSettings) -> PlannedWork:
    """Ask the catalog which fragments should merge. Raises `CompactionPlaneUnavailable` when the door cannot answer.

    Raising rather than returning an empty plan is the load-bearing choice: an empty plan MEANS the
    table is already at target, and a door outage that borrowed that spelling would report every
    unreachable table as healthy — the sweep's most expensive silent failure.

    A 401 raises `MaintenanceUnauthenticated` (this service's credential) and a 403 `MaintenanceDenied` (the id).
    A 404 TableNotFound or NamespaceNotFound raises `TableNotGoverned`, worded from the door's answer:
    the door found nothing to plan under this id. Any other 4xx raises `CompactionPlanRefused`, logged at ERROR: the door parsed the request
    and found it wrong, so the fault is this executor's and an in-pod rewrite would only hide it.
    """
    url = f"{settings.catalog_url.rstrip('/')}/management/v1/table/{table_id}/compaction_plan"
    try:
        response = httpx.post(url, json=policy or {}, headers=service_headers(settings), timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise CompactionPlaneUnavailable(f"compaction plan unreachable for {table_id}: {exc}") from exc
    # A refusal is never `CompactionPlaneUnavailable`, which the caller answers by compacting in-pod: the
    # rewrite the door refused would run anyway. Both classes below are `MaintenanceDenied`.
    if response.status_code == 401:
        raise MaintenanceUnauthenticated(f"no compaction plan: {unauthenticated_remedy(table_id=table_id, identity=settings.catalog_service_identity)}")
    if response.status_code == 403:
        raise MaintenanceDenied(
            f"the catalog REFUSED a compaction plan for {table_id} ({response.status_code}) — this rewrite is not "
            f"authorized. {denial_remedy(table_id=table_id, identity=settings.catalog_service_identity)}"
        )
    if 400 <= response.status_code < 500 and response.status_code not in _RETRY_LATER:
        if (absent := table_not_governed(response, table_id=table_id)) is not None:
            raise absent
        _, detail = _problem(response)
        log.error("compaction_plan_refused", extra={"table_id": table_id, "status": response.status_code, "detail": detail})
        raise CompactionPlanRefused(
            f"the catalog refused the compaction plan request for {table_id} as malformed ({response.status_code}): {detail}",
            status=response.status_code,
            detail=detail,
        )
    if response.status_code >= 400:
        raise CompactionPlaneUnavailable(f"compaction plan unavailable for {table_id} ({response.status_code}): {response.text[:200]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CompactionPlaneUnavailable(f"compaction plan returned an unparseable body for {table_id}") from exc
    return PlannedWork(read_version=int(payload.get("read_version", 0)), tasks=[str(task) for task in payload.get("tasks") or []])


def commit_via_catalog(table_id: str, results: list[str], *, settings: MaintenanceSettings) -> CommittedWork:
    """Fold the rewrite results into one metadata-only version. Raises when the door cannot answer.

    No fallback exists for this half and none should be invented: the data files the results name are
    already on the store, so a caller that swallowed the failure would leave them unreferenced and
    report a compaction that never happened.
    """
    url = f"{settings.catalog_url.rstrip('/')}/management/v1/table/{table_id}/compaction_commit"
    try:
        response = httpx.post(url, json={"results": results}, headers=service_headers(settings), timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise DistributedCompactionError(
            f"compaction commit unreachable for {table_id} — {len(results)} rewrite(s) are written and unreferenced: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise DistributedCompactionError(
            f"compaction commit refused for {table_id} ({response.status_code}) — {len(results)} rewrite(s) are written and unreferenced: {response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise DistributedCompactionError(f"compaction commit returned an unparseable body for {table_id}") from exc
    return CommittedWork(
        version=int(payload.get("version", 0)),
        fragments_added=int(payload.get("fragments_added", 0)),
        fragments_removed=int(payload.get("fragments_removed", 0)),
    )
