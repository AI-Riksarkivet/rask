"""Finishing the ends of registered clones their doors could not finish (ADR 0198 §1), once per audit.

A door ending a registered clone files the clone's ending before its row goes and removes it once the clone's tags on
its sources are released. One it could not remove (the store refused a source's tag delete, or the process died) would
keep each source from being destroyed for good. The catalog finishes it on its own reads, at its finish door; maintenance
only asks, holding no write on a source's ``_refs/tags``. Asked for every ending each audit lists, so a stranded tag
lasts at most an audit past a claim's lease. A refusal is the catalog's word (too young, being finished, or the clone
still stands), logged by the ending's id at WARNING and asked again next audit; never fatal to the audit. An ending
whose clone stands is removed by the catalog a day after its filing, so no refusal repeats for longer.
"""

from __future__ import annotations

import logging

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from maintenance.core.config import MaintenanceSettings
from maintenance.services.credentials import MaintenanceDenied, TableNotGoverned, VendUndecided, finish_clone_ending, list_clone_endings


log = logging.getLogger(__name__)


class CloneEndingsReport(BaseModel):
    """One audit's finishes: the endings finished, those the clone kept by standing again, and those refused, with why."""

    finished: list[str] = Field(default_factory=list)
    kept: list[str] = Field(default_factory=list)
    refused: dict[str, str] = Field(default_factory=dict)
    #: Why no ending was listed, when the listing itself was refused or could not be asked.
    reason: str | None = None


async def finish_clone_endings(settings: MaintenanceSettings) -> CloneEndingsReport:
    """Ask the catalog to finish every clone's ending it lists, logged NAMED at WARNING when it did or refused anything."""
    report = CloneEndingsReport()
    try:
        endings = await run_in_threadpool(list_clone_endings, settings)
    except (MaintenanceDenied, VendUndecided) as exc:
        report.reason = str(exc)
        log.warning("clone_endings_unlisted", extra={"reason": report.reason})
        return report
    for ending in endings:
        try:
            finished = await run_in_threadpool(finish_clone_ending, ending, settings)
        except TableNotGoverned:
            continue
        except (MaintenanceDenied, VendUndecided) as exc:
            report.refused[ending.id] = str(exc)
            continue
        (report.kept if finished.kept else report.finished).append(ending.id)
    if report.finished or report.kept or report.refused:
        log.warning("clone_endings_result", extra=report.model_dump(mode="json"))
    return report
