"""Removing a trash record once the bytes it points at are PROVEN gone ([[LH-061]]'s storage tier).

**IT DELETES NO BYTES.** Every other reading of "act on the storage tier" does — reclaiming orphan
files, dropping an unclaimed bucket — and each of those can destroy something unrecoverable. This one
deletes a REGISTRY RECORD, and only where the thing it describes has already ceased to exist.

WHY THAT IS THE RIGHT FIRST CUT, and why it does not contradict `repair.py`. That module refuses
`orphaned_trash` by name, with the correct reason: a trash record is the only remaining POINTER to the
bytes it describes, so dropping it blind strands them silently. This pass does not overturn that
refusal — it SATISFIES it. The objection is an assumption about the bytes; probing the location turns
it into a measurement, and a record that still names objects is refused here exactly as it is there.

WHAT IS ACTUALLY OUT THERE, measured in-pod 2026-09-20: all 989 orphaned trash records point at a
bucket holding nothing, across 63 buckets — 62 empty or absent, and the single bucket that still holds
objects belongs to a LIVE warehouse whose records are not orphaned at all. Their bytes went with the
deleted warehouse's bucket ([[LH-148]]'s cascade, fixed at source). What survives is a promise of
recovery the estate can no longer perform and a purge slot it can never use.

THE ZERO IS NOT THE CONTRACT. 989/989 being tombstones today is why this is worth doing, not why it is
safe; the refusals are why it is safe, and both are pinned:

* a location that still holds objects is REFUSED and the count is named;
* a probe that RAISES is refused too — "we could not tell" reads as the refusal for the reason
  `maintenance.require_reclaimable`'s base guard gives, because what it guards is unrecoverable.

CAPPED PER TICK because the probe is one listing PER RECORD. Sweeping 989 in a tick would turn a
reconcile pass into a whole-estate scan, which is the cost this service exists to bound.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.reconcile import ReconcileReport


log = logging.getLogger(__name__)

#: Counts the objects at a trash record's location. Injected rather than imported so the DECISION is
#: testable without a store: what is worth pinning here is which records are swept and which refused,
#: and a suite that had to stand up S3 to ask that would be run rarely and trusted less.
Probe = Callable[[str], int]


class Tombstone(BaseModel):
    """One trash record this pass would remove, or removed."""

    id: str
    kind: str
    location: str
    #: What the probe actually found — the evidence, carried so a reader can check the claim rather
    #: than take "it was empty" on trust. Always 0 for a swept record; that is the whole condition.
    probed_objects: int


class TombstoneReport(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    swept: list[Tombstone] = Field(default_factory=list)
    #: Records considered and declined, by id, with why. Present rather than silent: "considered and
    #: refused" and "never looked" are different facts about a cleaner, and only one of them is safe.
    refused: dict[str, str] = Field(default_factory=dict)
    #: Eligible records beyond `max_per_tick`, so a truncated pass cannot read as a complete one.
    capped: int = 0
    error: str | None = None


def plan_sweep(report: ReconcileReport, *, probe: Probe) -> tuple[list[Tombstone], dict[str, str]]:
    """The orphaned trash records whose bytes are proven gone, and the ones refused with their reason.

    SCOPED TO THE REPORT'S FINDINGS, never a fresh scan of the trash prefix: the justification for
    touching a record at all is the report's own finding that no live warehouse claims its bucket, so
    a second independent read could sweep a record the report never classified.
    """
    planned: list[Tombstone] = []
    refused: dict[str, str] = {}
    for finding in report.orphaned_trash or []:
        try:
            objects = probe(finding.location)
        except Exception as exc:  # noqa: BLE001 — an unreadable location is a refusal, never a sweep
            refused[finding.id] = f"the location {finding.location!r} could not be read ({type(exc).__name__}), so nothing proves its bytes are gone"
            continue
        if objects:
            refused[finding.id] = (
                f"{finding.location!r} still holds {objects} object(s) — this record is the only remaining pointer to them, "
                "so removing it would strand them silently"
            )
            continue
        planned.append(Tombstone(id=finding.id, kind=finding.kind, location=finding.location, probed_objects=objects))
    return planned, refused


def sweep_tombstones(
    settings: MaintenanceSettings,
    *,
    report: ReconcileReport,
    probe: Probe,
    delete: Callable[[Tombstone], None],
) -> TombstoneReport:
    """Remove the trash records whose bytes the probe proves are already gone.

    Degrades rather than fails, the same contract `rebuild.py` and `repair.py` carry: one record that
    will not delete leaves `error` set and the tick continues, because a reconcile tick that 500s over
    a failed cleanup is worse than one reporting residue it could not clear — the report is what
    everything else is gated on.
    """
    out = TombstoneReport(enabled=settings.tombstone_sweep_enabled, dry_run=settings.tombstone_sweep_dry_run)
    if not settings.tombstone_sweep_enabled:
        return out
    # CAPPED BEFORE PROBING, not after. The probe is the expensive half — one listing per record — so
    # planning all 989 and then discarding 939 would pay the whole cost to do a fiftieth of the work.
    findings = list(report.orphaned_trash or [])
    out.capped = max(0, len(findings) - settings.tombstone_sweep_max_per_tick)
    head = report.model_copy(update={"orphaned_trash": findings[: settings.tombstone_sweep_max_per_tick]})
    planned, out.refused = plan_sweep(head, probe=probe)
    if not planned or out.dry_run:
        out.swept = planned
        return out
    done: list[Tombstone] = []
    for target in planned:
        try:
            delete(target)
        except Exception as exc:  # noqa: BLE001 — see the docstring; one record must not fail the tick
            log.warning("tombstone_sweep_failed", extra={"record": target.id, "location": target.location, "error": str(exc)})
            out.error = str(exc)
            continue
        done.append(target)
    out.swept = done
    return out
