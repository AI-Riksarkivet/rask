#!/usr/bin/env python3
"""Compact each table the catalog lists as due, by hand, through the catalog's doors (ADR 0162, ADR 0174).

The lakehouse builds nothing: maintenance decides which tables a compaction is due on, the catalog lists them on
``GET /management/v1/maintenance/due`` to whoever holds ``can_maintain`` on each, and the compaction runs on the
caller's compute and commits through ``version/create``, which judges each commit. A scheduled job doing that is
bring-your-own and lives outside this repository. This script is the same caller run by a person, for a development
estate that has no such job: it reads the list as the bearer it is given, and runs Lance's own compaction protocol
(``Compaction.plan``, each task, ``Compaction.commit``: a ``ReserveFragments`` and a ``Rewrite``) on every table the
list names, at the fragment size the winning policy set. Stable row ids survive a compaction
(`lance_docs/lance/format/table/row_id_lineage.md`), so no cursor on lineage's feed moves.

Why a person would run it: every commit to a provenance table adds a fragment, and each ``_rowid``-filtered read of
one costs per fragment. Measured on the dev estate 2026-11-13, ``provenance$events`` at 935 fragments answered such a
read in 213 ms against 12 ms at 2.

It compacts and does nothing else: index work the list names is the index owner's build, and version expiry is
maintenance's, which the catalog's announcement of each compaction commit sets off.

Usage::

    COMPACT_CATALOG_TOKEN="$(...the person's OIDC id_token...)" uv run python scripts/compact_due_tables.py
    uv run python scripts/compact_due_tables.py --catalog-url http://localhost:8080/api/catalog --only 'lance_catalog$provenance'

Env (argparse overrides both)::

    COMPACT_CATALOG_URL    the catalog's public base (default http://localhost:8080/api/catalog)
    COMPACT_CATALOG_TOKEN  the caller's OIDC bearer; empty = an auth-off local stack
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Final

import httpx
import lance
import lance_namespace
from lance.optimize import Compaction
from pydantic import BaseModel, ConfigDict, Field


#: The Lance Namespace spec's identifier delimiter, which the due list joins a table's name with.
_DELIMITER: Final = "$"
#: The due list's page size: the door's own ceiling is 1000.
_PAGE: Final = 100
#: How long one catalog request of the due list may take.
_TIMEOUT_SECONDS: Final = 60.0


class DueCompaction(BaseModel):
    """What made a table due for a compaction, as far as this script needs it."""

    model_config = ConfigDict(extra="ignore")

    fragments: int | None = None
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


def due_tables(client: httpx.Client) -> list[DueTable]:
    """Every table the due list names to this caller, page by page."""
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


def compact(catalog_url: str, headers: dict[str, str], due: DueTable) -> tuple[int, int]:
    """Run Lance's own compaction of ``due``'s table through the catalog; its fragment count before and after."""
    properties = {"uri": catalog_url, **{f"headers.{name}": value for name, value in headers.items()}}
    namespace = lance_namespace.connect("rest", properties)
    table_id = due.table.split(_DELIMITER)
    dataset = lance.dataset(namespace_client=namespace, table_id=table_id)
    before = len(dataset.get_fragments())
    target = due.compaction.target_rows_per_fragment if due.compaction is not None else None
    plan = Compaction.plan(dataset, {"target_rows_per_fragment": target} if target is not None else {})
    if not plan.tasks:
        return before, before
    Compaction.commit(dataset, [task.execute(dataset) for task in plan.tasks])
    return before, len(lance.dataset(namespace_client=namespace, table_id=table_id).get_fragments())


def main() -> int:
    """One run: compact each due table the caller may maintain; non-zero when any failed or the list could not be read."""
    parser = argparse.ArgumentParser(description="Compact each table the catalog lists as due, through the catalog's doors.")
    parser.add_argument("--catalog-url", default=os.environ.get("COMPACT_CATALOG_URL", "http://localhost:8080/api/catalog"))
    parser.add_argument("--token", default=os.environ.get("COMPACT_CATALOG_TOKEN", ""), help="the caller's OIDC bearer (empty = auth off)")
    parser.add_argument(
        "--only",
        default="",
        help="compact only the tables under this warehouse or namespace, named in full from the warehouse, e.g. 'lance_catalog$provenance'",
    )
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}

    with httpx.Client(base_url=args.catalog_url.rstrip("/"), headers=headers, timeout=_TIMEOUT_SECONDS) as client:
        listed = due_tables(client)
    chosen = [due for due in listed if due.compaction is not None and (not args.only or due.table.startswith(f"{args.only}{_DELIMITER}"))]
    print(f"due list: {len(listed)} tables, {len(chosen)} to compact")
    failed = 0
    for due in chosen:
        try:
            before, after = compact(args.catalog_url.rstrip("/"), headers, due)
        # Any exception: one table's refusal or outage must not stop the rest of the run, which reports it.
        except Exception as exc:
            failed += 1
            print(f"  FAILED    {due.table}: {type(exc).__name__}: {str(exc)[:300]}")
            continue
        print(f"  {'compacted' if after != before else 'unchanged'} {due.table}: {before} -> {after} fragments")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
