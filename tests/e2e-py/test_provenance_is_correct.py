"""Criterion 1, provenance and lineage correct, read off one drive of the shared scenario (XC-091, `byo_scenario`).

Provenance is who wrote each commit, under which run, from which inputs (ADR 0158). Lance holds the ground truth: a
table's version histories, main's and each branch's, and the `rask.*` transaction properties each commit carries
(`read_transaction(v)`; `lance_docs/PROVENANCE.md` § Transaction properties, `catalog.core.run_context`). Lineage records
the table-level edge list from each commit's announcement: one row for each input a run names, from the version it read
to the version and ref it wrote. This module holds Lance to what the pipeline did, and the edge list to Lance:

1. every version of every ref of the scenario's tables is a commit the pipeline made, and carries exactly the run it
   named and the person who committed it: bronze's stock seed and silver's append on `staging` stage their runs through
   `version/create`, and silver's create and insert name theirs, with bronze at the version they read, in the spec
   Context of the catalog's doors;
2. the edge list holds exactly one edge for each input a version names, to that version on its ref, on the commit's run
   and by its author, and no other edge names either table.

A branch is its own version history (`lance_docs/file_format.md` § Branch and Tag Specification): its manifests and
transactions live under `tree/<branch>/`, and its history starts after the parent version `_refs/branches/<branch>.json`
records, which the branch lists as the clone it was cut from. So the walk takes the branches Lance lists from `_refs`,
never by listing `tree/`, and reads each one's versions past that parent version.

The edge list is read once lineage has acknowledged every message the bus held, so a missing or a duplicated edge is
lineage's decision, never an announcement still in flight; the read then waits out an announcement the catalog left in
its outbox for the relay. It is read the way its owner reads any table, by a stock client opening `provenance$edges`
through the namespace: the estate's owner owns the provenance namespace through the estate's default warehouse
(`catalog.services.provenance_namespace`).

WHAT IS NOT CLAIMED, and why. That each commit is announced once, by its author, is criterion 4's
(`test_events_are_correct.py`). Lineage's one door over the edge list, `/tables/{t}/derived`, answers a
set of tables, so a duplicated or a missing edge is invisible there.

Run with `bash scripts/e2e_live.sh -m phase1 --require-live` against the deployed estate, `make e2e-phase1` with the
addresses set, or the e2e-stack lane, which runs it on every push.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Mapping
from typing import Any

import bus
import byo_scenario as scenario
import lance
import pytest
from byo_scenario import ScenarioRun, Tenant
from pydantic import BaseModel, ConfigDict


#: The namespace the catalog keeps lineage's tables in (`services.lineage.provenanceNamespace`).
PROVENANCE_NAMESPACE = os.environ.get("LANCE_E2E_PROVENANCE_NAMESPACE", "provenance")
#: The durable consumer through which lineage records what the catalog announces.
LINEAGE_DURABLE = "lineage-durable"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.phase1,
    pytest.mark.skipif(not (scenario.LIVE and bus.NATS), reason=f"{scenario.REQUIRES}, and LANCE_E2E_NATS_URL (the bus)"),
]

#: Each version of the scenario's tables, as ``(table, branch, version)`` with ``None`` for main, and the `rask.*`
#: properties its commit carries.
type History = dict[tuple[str, str | None, int], dict[str, Any]]


class Edge(BaseModel):
    """One row of the edge list, without its key, its announcement's run id or its times."""

    model_config = ConfigDict(frozen=True)

    in_table: str
    in_version: int | None
    in_branch: str | None
    out_table: str
    out_version: int | None
    out_branch: str | None
    writer_run_id: str | None
    author: str | None


def _rask(properties: Mapping[str, str]) -> dict[str, Any]:
    """The `rask.*` entries of a commit's transaction properties, `rask.inputs` decoded."""
    rask: dict[str, Any] = {key: value for key, value in properties.items() if key.startswith("rask.")}
    if "rask.inputs" in rask:
        rask["rask.inputs"] = json.loads(rask["rask.inputs"])
    return rask


def _edges_named(history: History) -> list[Edge]:
    """The edges Lance's history asserts: one for each input a commit names, from the version read to the version
    written on its ref, on the commit's run and by its author."""
    return [
        Edge(
            in_table="$".join(source["table"]),
            in_version=source.get("version"),
            in_branch=source.get("branch"),
            out_table=table,
            out_version=version,
            out_branch=branch,
            writer_run_id=stamped.get("rask.run_id"),
            author=stamped.get("rask.author"),
        )
        for (table, branch, version), stamped in history.items()
        for source in stamped.get("rask.inputs", [])
    ]


def _refs(main: lance.LanceDataset) -> list[tuple[str | None, lance.LanceDataset, int]]:
    """Each ref of a table as ``(branch, its head, the version its own history starts after)``: main from the start, then
    each branch Lance lists under `_refs/branches`, after the parent version it was cut at."""
    branches = main.branches.list()
    return [(None, main, 0), *((name, main.checkout_version((name, None)), int(info["parent_version"])) for name, info in branches.items())]


def _sql_list(names: tuple[str, ...]) -> str:
    """``names`` as a SQL list of string literals for a Lance filter."""
    return ", ".join("'" + name.replace("'", "''") + "'" for name in names)


@pytest.fixture(scope="module")
def history(byo_tenant: Tenant, byo_run: ScenarioRun) -> History:
    """Every version of every ref of the scenario's two tables and what its commit carries, read by the pipeline that
    owns them."""
    found: History = {}
    for table in (byo_run.bronze_table, byo_run.silver_table):
        main = lance.dataset(namespace_client=byo_tenant.pipeline.namespace(), table_id=table.split("$"))
        for branch, head, parent_version in _refs(main):
            own_versions = [int(listed["version"]) for listed in head.versions() if int(listed["version"]) > parent_version]
            for version in own_versions:
                transaction = head.read_transaction(version)
                assert transaction is not None, f"{table} version {version} on {branch or 'main'} has no transaction to read"
                found[(table, branch, version)] = _rask(transaction.transaction_properties or {})
    return found


@pytest.fixture(scope="module")
def edges(byo_tenant: Tenant, byo_run: ScenarioRun, history: History) -> list[Edge]:
    """The edge list's rows naming either scenario table, read as the estate's owner: first once lineage has
    acknowledged every message the bus holds, then again while the history names an edge they lack and the settle
    window lasts, for an announcement the relay re-publishes from the catalog's outbox."""
    bus.wait_until_acked("LINEAGE", (LINEAGE_DURABLE,), bus.last_seq("LINEAGE"), within=scenario.SETTLE_SECONDS)
    tables = _sql_list((byo_run.bronze_table, byo_run.silver_table))
    expected = Counter(_edges_named(history))
    deadline = time.monotonic() + scenario.SETTLE_SECONDS
    while True:
        edge_list = lance.dataset(namespace_client=byo_tenant.admin.namespace(), table_id=[*PROVENANCE_NAMESPACE.split("$"), "edges"])
        rows = edge_list.to_table(columns=list(Edge.model_fields), filter=f"in_table IN ({tables}) OR out_table IN ({tables})").to_pylist()
        recorded = [Edge.model_validate(row) for row in rows]
        if expected <= Counter(recorded) or time.monotonic() > deadline:
            return recorded
        time.sleep(2)


def test_each_version_is_a_pipeline_commit_carrying_the_run_it_named_and_who_committed_it(history: History, byo_tenant: Tenant, byo_run: ScenarioRun) -> None:
    """The pipeline made every commit and named each one's run. Its stock commits, bronze's seed and silver's append on
    `staging`, carry the run as the pipeline staged it, which `version/create` admits only under the caller's own
    principal; silver's create and insert carry the run the catalog stamped from the spec Context, beside the person
    who called its door."""
    author = byo_tenant.pipeline.principal

    assert history == {(commit.table, commit.branch, commit.version): _rask(commit.run.transaction_properties(author=author)) for commit in byo_run.commits}


def test_the_edge_list_holds_one_edge_for_each_input_a_version_names_and_no_other(history: History, edges: list[Edge]) -> None:
    """Compared as multisets, so an edge recorded twice is not an edge recorded once."""
    expected = _edges_named(history)

    assert expected, "no version names an input, so there is no edge to hold the list to"
    assert Counter(edges) == Counter(expected)
