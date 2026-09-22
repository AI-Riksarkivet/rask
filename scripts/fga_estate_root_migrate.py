"""Carry the estate PRIVILEGES from the default warehouse onto the estate root ([[LH-055]]).

`fga_root_object` moves from `warehouse:lance_catalog` to `estate:rask`, and every check aimed at it
moves with it: `can_observe_events` (the admin console, the whole-estate lineage projection),
`can_browse_storage` (the object browser), `can_stage_events` (the lineage outbox vend), and the bare
`reader` / `writer` that compute, controlplane and flows gate their whole routers on.

THE TUPLES DO NOT MOVE THEMSELVES. A principal holding `owner` on the old root passes every one of
those doors today and NONE of them the moment the setting flips, so the grants have to exist on the new
object BEFORE the repoint is deployed — which is why this is a script that can run against a live store
rather than a note in the row. The chart's bootstrap hook seeds `auth.bootstrapAdmin` and the stagers;
this covers everyone else, including the subjects a running estate acquired outside the chart.

NOTHING IS REMOVED. The old tuples keep governing the WAREHOUSE, which is their other and still-correct
job: `warehouse:lance_catalog` parents the estate's top-level namespaces, so `owner` there is what makes
the medallion cascade reach bronze/silver/gold. A revoke would take that with it.

DIRECT TUPLES ONLY, and that is a TIGHTENING rather than an oversight. `warehouse#reader` resolves
`or member from project`, so on an estate that binds the default warehouse to a project every member of
that project passes the estate doors — estate-wide read conferred by tenant membership, which is the
conflation this port exists to end. Such holders are not carried over; the run REPORTS whether any
exist rather than deciding for the operator.

Usage — from inside a pod that can reach OpenFGA (the address the code reads, never a port-forward)::

    make fga-estate-migrate            # apply
    make fga-estate-migrate DRY_RUN=1  # report only
"""

from __future__ import annotations

import os
import sys
from typing import Any, Final

import httpx


#: Old rung -> new rung. Each maps to ITSELF rather than a promotion, and the list is SHORT on purpose.
#:
#: `owner` is not mapped to `admin`: `estate#admin` additionally carries `can_create_project` and the
#: `operator` machine tier, so promoting on migration would hand every current warehouse owner two
#: authorities nobody granted them. `event_stager` is the one SERVICE rung that follows the root, for
#: the reason it was minted on the root to begin with — `can_stage_events` guards a platform control
#: prefix, not anyone's data.
RUNG_MAP: Final[dict[str, str]] = {"owner": "owner", "event_stager": "event_stager"}

#: Warehouse rungs that are DELIBERATELY not carried, and what carrying one would have granted.
#:
#: This is the whole point of the port rather than a gap in it. Measured on the live store 2026-09-22,
#: a blanket carry would have written `estate#writer` for four cascade services — `service-ingest`,
#: `service-medallion-producer`, `service-bronze-to-silver`, `service-media-to-silver` — and
#: `estate#writer` is what flows gates run/terminate on. A service that may append bronze rows to one
#: warehouse would have acquired the authority to execute flows across the estate, purely as a
#: side-effect of a setting being repointed. Verified before narrowing: none of those services, nor
#: `notifications`, calls compute, controlplane or flows, so nothing in use is lost.
#:
#: `maintainer` is absent from both lists because `type estate` does not define it at all — it is a
#: warehouse rung describing who may rewrite bytes, which the estate root has no opinion about.
NOT_CARRIED: Final[dict[str, str]] = {
    "writer": "estate#writer is what flows gates run/terminate/read on",
    "reader": "estate#reader is what compute and controlplane gate their whole routers on",
    "maintainer": "type estate does not define it — it is a warehouse rung",
}

OLD_ROOT: Final = os.environ.get("RASK_FGA_DEFAULT_WAREHOUSE_OBJECT", "warehouse:lance_catalog")
NEW_ROOT: Final = os.environ.get("RASK_FGA_ESTATE_OBJECT", "estate:rask")


def _post(api: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST and RAISE on anything but 2xx — a migration that swallows a refusal reports success."""
    response = httpx.post(f"{api}{path}", json=body, timeout=30)
    response.raise_for_status()
    decoded: dict[str, Any] = response.json()
    return decoded


def _get(api: str, path: str) -> dict[str, Any]:
    response = httpx.get(f"{api}{path}", timeout=30)
    response.raise_for_status()
    decoded: dict[str, Any] = response.json()
    return decoded


def _principals_on(api: str, store_id: str, obj: str) -> dict[str, list[str]]:
    """Every tuple whose OBJECT is `obj`, grouped by relation.

    PAGE SIZE 100 is the API's ceiling, not a preference: OpenFGA answers a larger `page_size` with a
    bare 400 naming nothing, which reads as an unreachable store.
    """
    held: list[dict[str, Any]] = []
    token = ""
    while True:
        body: dict[str, Any] = {"tuple_key": {"object": obj}, "page_size": 100}
        if token:
            body["continuation_token"] = token
        page = _post(api, f"/stores/{store_id}/read", body)
        held.extend(page.get("tuples") or [])
        token = str(page.get("continuation_token") or "")
        if not token:
            break

    by_relation: dict[str, list[str]] = {}
    for entry in held:
        key: dict[str, Any] = entry["key"]
        by_relation.setdefault(str(key["relation"]), []).append(str(key["user"]))
    return by_relation


def _apply(api: str, store_id: str, writes: list[dict[str, str]]) -> int:
    for write in writes:
        # CHECK FIRST, exactly as the bootstrap hook does: a write that duplicates an existing tuple is
        # a 400 on this API, and an unconditional loop would turn a re-run into a failure.
        if _post(api, f"/stores/{store_id}/check", {"tuple_key": write}).get("allowed"):
            print(f"   already held: {write['user']} {write['relation']} {NEW_ROOT}")
            continue
        try:
            _post(api, f"/stores/{store_id}/write", {"writes": {"tuple_keys": [write]}})
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            if exc.response.status_code in (400, 409) and "already exists" in detail:
                print(f"   raced, already written: {write['user']} {write['relation']}")
                continue
            print(f"!! write failed for {write}: HTTP {exc.response.status_code} {detail}", file=sys.stderr)
            return 1
        print(f"   granted {write['user']} {write['relation']} {NEW_ROOT}")
    return 0


def main() -> int:
    api = os.environ.get("RASK_FGA_API_URL", "").rstrip("/")
    if not api:
        print("!! RASK_FGA_API_URL is unset — run this where the code's own OpenFGA address resolves", file=sys.stderr)
        return 1
    dry_run = bool(os.environ.get("DRY_RUN"))

    stores: list[dict[str, Any]] = _get(api, "/stores").get("stores") or []
    if not stores:
        print("!! the OpenFGA instance holds no store")
        return 1
    store_id = str(stores[0]["id"])
    by_relation = _principals_on(api, store_id, OLD_ROOT)

    if by_relation.get("project"):
        print(f">> NOTE: {OLD_ROOT} is bound to {by_relation['project']} — every member of that project reads it")
        print("   transitively and passes the estate doors today. Those holders are NOT carried over.")

    writes: list[dict[str, str]] = []
    for old_rung, new_rung in RUNG_MAP.items():
        for user in by_relation.get(old_rung, []):
            if user.split(":", 1)[0] not in {"user", "role"}:
                # A structural tuple (`namespace:x child`, `project:y project`) is not a principal.
                continue
            writes.append({"user": user, "relation": new_rung, "object": NEW_ROOT})

    for rung, why in NOT_CARRIED.items():
        holders = [u for u in by_relation.get(rung, []) if u.split(":", 1)[0] in {"user", "role"}]
        if holders:
            print(f">> NOT carried: {len(holders)} holder(s) of {OLD_ROOT}#{rung} — {why}")
            for holder in holders:
                print(f"   {holder}")

    if not writes:
        print(f">> nothing to carry: {OLD_ROOT} holds no principal on {sorted(RUNG_MAP)}")
        return 0

    print(f">> {len(writes)} grant(s) to carry from {OLD_ROOT} to {NEW_ROOT}:")
    for write in writes:
        print(f"   {write['user']} {write['relation']}")
    if dry_run:
        print(">> DRY_RUN — nothing written")
        return 0
    return _apply(api, store_id, writes)


if __name__ == "__main__":
    raise SystemExit(main())
