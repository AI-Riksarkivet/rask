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
  to the in-pod rewrite — nothing was planned, so nothing was written;
* a COMMIT that does not answer RAISES. The bytes are already written; reporting success would leave
  a table that looks compacted, is not, and has orphans nobody will attribute.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from maintenance.services.compaction_executor import CommittedWork, CompactionPlaneUnavailable, DistributedCompactionError, PlannedWork
from service_kit.governed.dapr_auth import DaprDoorSettings


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings


log = logging.getLogger(__name__)

#: The plan is a manifest read and the commit is one metadata transaction — neither is unbounded in
#: the data, so a call that blocks longer than this is a door in trouble rather than a big table.
_TIMEOUT_SECONDS = 30.0


def _headers(settings: MaintenanceSettings) -> dict[str, str]:
    """Both halves of the service identity, or the door refuses with a reason invisible from here."""
    headers = {"x-lance-service-identity": settings.catalog_service_identity}
    if token := DaprDoorSettings().app_api_token:
        headers["dapr-api-token"] = token
    return headers


def plan_via_catalog(table_id: str, policy: dict[str, Any], *, settings: MaintenanceSettings) -> PlannedWork:
    """Ask the catalog which fragments should merge. Raises `CompactionPlaneUnavailable` when the door cannot answer.

    Raising rather than returning an empty plan is the load-bearing choice: an empty plan MEANS the
    table is already at target, and a door outage that borrowed that spelling would report every
    unreachable table as healthy — the sweep's most expensive silent failure.
    """
    url = f"{settings.catalog_url.rstrip('/')}/v1/table/{table_id}/compaction_plan"
    try:
        response = httpx.post(url, json=policy or {}, headers=_headers(settings), timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise CompactionPlaneUnavailable(f"compaction plan unreachable for {table_id}: {exc}") from exc
    if response.status_code >= 400:
        raise CompactionPlaneUnavailable(f"compaction plan refused for {table_id} ({response.status_code}): {response.text[:200]}")
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
    url = f"{settings.catalog_url.rstrip('/')}/v1/table/{table_id}/compaction_commit"
    try:
        response = httpx.post(url, json={"results": results}, headers=_headers(settings), timeout=_TIMEOUT_SECONDS)
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
