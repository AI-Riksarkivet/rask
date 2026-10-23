"""Compact each table the catalog lists as due that this job may maintain (ADR 0162).

The lakehouse builds nothing: the maintenance sweep decides which tables a compaction is due on, the catalog lists them
on ``GET /management/v1/maintenance/due`` to whoever holds ``can_maintain`` on each, and the compaction runs on the
caller's compute and commits through ``version/create``, which judges each commit. This job is such a caller, as its
own service-account principal: it reads the list, and runs Lance's own compaction protocol (``Compaction.plan``, each
task, ``Compaction.commit``: a ``ReserveFragments`` and a ``Rewrite``) on every table the list names, at the fragment
size the winning policy set.

It compacts and does nothing else: index work the list names is the index owner's build, and version expiry is
maintenance's, which the catalog's announcement of each compaction commit sets off.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Final

import httpx
import lance
import lance_namespace
from lance.optimize import Compaction
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


log = logging.getLogger(__name__)

#: The Lance Namespace spec's identifier delimiter, which the due list joins a table's name with.
_DELIMITER: Final = "$"
#: The due list's page size: the door's own ceiling is 1000.
_PAGE: Final = 100


class JobSettings(BaseSettings):
    """Where the catalog is, and the token this job presents to it."""

    model_config = SettingsConfigDict(env_prefix="TABLE_MAINTENANCE_", extra="ignore")

    #: The catalog's base URL, e.g. ``http://rask-catalog:2333``.
    catalog_url: str
    #: The service-account token the kubelet projects for the catalog's audience. Read afresh for each table: the
    #: kubelet rotates it before it expires, and a run may outlast one token.
    token_file: Path
    #: How long one catalog request may take before the run reports the table failed.
    timeout_seconds: float = Field(default=60.0, gt=0)


class DueCompaction(BaseModel):
    """What made a table due for a compaction, as far as this job needs it."""

    model_config = ConfigDict(extra="ignore")

    #: The fragment size the winning policy set, ``None`` for Lance's own.
    target_rows_per_fragment: int | None = None


class DueTable(BaseModel):
    """One entry of the due list."""

    model_config = ConfigDict(extra="ignore")

    table: str
    compaction: DueCompaction | None = None


class DuePage(BaseModel):
    """One page of the due list."""

    model_config = ConfigDict(extra="ignore")

    tables: list[DueTable] = Field(default_factory=list)
    page_token: str | None = None


class Run(BaseModel):
    """What one run did, by table name."""

    compacted: list[str] = Field(default_factory=list)
    #: Due on nothing this job does (index work alone), or nothing left to compact when the run reached it.
    unchanged: list[str] = Field(default_factory=list)
    #: The tables whose compaction raised, with what it raised.
    failed: dict[str, str] = Field(default_factory=dict)


def due_tables(client: httpx.Client) -> list[DueTable]:
    """Every table the due list names to this job, page by page."""
    tables: list[DueTable] = []
    token: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": _PAGE, **({"page_token": token} if token else {})}
        answer = client.get("/management/v1/maintenance/due", params=params)
        answer.raise_for_status()
        page = DuePage.model_validate(answer.json())
        tables.extend(page.tables)
        if not page.page_token:
            return tables
        token = page.page_token


def bearer(settings: JobSettings) -> str:
    """The ``Authorization`` value this job presents: its current service-account token."""
    return f"Bearer {settings.token_file.read_text().strip()}"


def compact(settings: JobSettings, due: DueTable) -> bool:
    """Run Lance's own compaction of ``due``'s table through the catalog; whether it rewrote anything."""
    namespace = lance_namespace.connect("rest", {"uri": settings.catalog_url, "headers.Authorization": bearer(settings)})
    dataset = lance.dataset(namespace_client=namespace, table_id=due.table.split(_DELIMITER))
    target = due.compaction.target_rows_per_fragment if due.compaction is not None else None
    plan = Compaction.plan(dataset, {"target_rows_per_fragment": target} if target is not None else {})
    if not plan.tasks:
        return False
    Compaction.commit(dataset, [task.execute(dataset) for task in plan.tasks])
    return True


def run(settings: JobSettings) -> Run:
    """Compact each due table this job may maintain; a table that fails is reported and the run goes on to the next.

    Raises:
        httpx.HTTPError: The due list could not be read, so the run did nothing.
    """
    ran = Run()
    with httpx.Client(base_url=settings.catalog_url, headers={"Authorization": bearer(settings)}, timeout=settings.timeout_seconds) as client:
        tables = due_tables(client)
    for due in tables:
        if due.compaction is None:
            ran.unchanged.append(due.table)
            continue
        try:
            rewrote = compact(settings, due)
        # Any exception: one table's refusal or outage must not stop the rest of the run, which reports it.
        except Exception as exc:
            log.warning("table_maintenance_failed", extra={"table": due.table, "error": f"{type(exc).__name__}: {exc}"})
            ran.failed[due.table] = f"{type(exc).__name__}: {exc}"
            continue
        (ran.compacted if rewrote else ran.unchanged).append(due.table)
        log.info("table_maintenance_compacted" if rewrote else "table_maintenance_nothing_to_compact", extra={"table": due.table})
    return ran


def main() -> None:
    """The CronJob's entrypoint: one run, exiting non-zero when any table failed or the due list could not be read."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ran = run(JobSettings.model_validate({}))
    log.info("table_maintenance_run", extra=ran.model_dump())
    sys.exit(1 if ran.failed else 0)
