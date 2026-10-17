"""A drive kept past its session, for Phase 1's soak to read again a day later (XC-090).

The acceptance (`-m phase1`) holds one drive of the bring-your-own scenario to the criteria once it has ended. The soak
(`test_the_estate_stays_correct_while_it_runs.py`) asks the same of the estate a day on: left running, with its
maintenance sweeps, outbox relays and reconcilers acting on what the drive made, does it still read the drive the way
the criteria did? So a run passed `--keep-drive PATH` keeps the scenario's tenant when every test it ran passed, and
records at PATH what the soak needs to read the drive again: the tenant, the run, who played each part, and what lineage's
feed and each person's inbox held of the drive once the session ended. A run passed `--soak PATH` resumes that tenant,
signed in again, and removes it once the soak passes.

The record is written before the tenant is marked kept, so a record that could not be written leaves the tenant to the
session's own teardown rather than keeping a tenant nothing names.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from byo_scenario import ScenarioRun, Tenant
from correctness import Announcement, Told, announced, told
from pydantic import BaseModel


class KeptDrive(BaseModel):
    """One drive and its tenant, as the session that kept it left them."""

    project: str
    #: Every table the session's pipeline made, which the tenant's removal drops.
    tables: list[str]
    created_at: datetime
    kept_at: datetime
    #: The principal of each person the scenario names, by part, so the soak reads as the same people.
    principals: dict[str, str]
    run: ScenarioRun
    feed: list[Announcement]
    told: dict[str, list[Told]]


def principals(tenant: Tenant) -> dict[str, str]:
    """The principal of each person ``tenant`` names, by the part each plays."""
    return {part: person.principal for part, person in tenant.people.items()}


def passed_whole(session: pytest.Session) -> bool:
    """Whether every test ``session`` has run so far passed, or failed as it was declared to: none failed, none skipped."""
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = getattr(reporter, "stats", {}).get("skipped", []) if reporter is not None else []
    return session.testsfailed == 0 and not skipped


def keep(tenant: Tenant, run: ScenarioRun, path: Path) -> None:
    """Record ``run`` on ``tenant`` at ``path``, with what the feed and the inboxes hold of it now, and keep the tenant."""
    drive = KeptDrive(
        project=tenant.project,
        tables=list(tenant.tables),
        created_at=tenant.created_at,
        kept_at=datetime.now(UTC),
        principals=principals(tenant),
        run=run,
        feed=announced(tenant.pipeline, run),
        told=told(tenant, run),
    )
    path.write_text(drive.model_dump_json(indent=2), encoding="utf-8")
    tenant.kept = True


def resume(path: Path) -> tuple[Tenant, KeptDrive]:
    """The tenant the drive at ``path`` was kept on, its people signed in again, and the record of the drive."""
    assert path.is_file(), f"no drive is kept at {path}: an acceptance run keeps one with `--keep-drive {path}`"
    drive = KeptDrive.model_validate_json(path.read_text(encoding="utf-8"))
    tenant = Tenant(project=drive.project)
    tenant.tables = list(drive.tables)
    tenant.created_at = drive.created_at
    return tenant, drive
