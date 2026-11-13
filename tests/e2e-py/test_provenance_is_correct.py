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
   and by its author, and no other edge names either table;
3. the feed lineage serves about the drive is the table `lance_catalog$provenance$events`: the provenance namespace is a
   namespace of the estate's own warehouse ([[LH-593]]), and a stock client reaches its tables by that full id.

How each ref's history and the edge list are read, branches included, is `correctness`'s, which criterion 5's drills
and the soak read the same way.

WHAT IS NOT CLAIMED, and why. That each commit is announced once, by its author, is criterion 4's
(`test_events_are_correct.py`). Lineage's one door over the edge list, `/tables/{t}/derived`, answers a
set of tables, so a duplicated or a missing edge is invisible there.

Run with `bash scripts/e2e_live.sh -m phase1 --require-live` against the deployed estate, `make e2e-phase1` with the
addresses set, or the e2e-stack lane, which runs it on every push.
"""

from __future__ import annotations

from collections import Counter

import bus
import byo_scenario as scenario
import lance
import pytest
from byo_scenario import ScenarioRun, Tenant
from correctness import PROVENANCE_NAMESPACE, Edge, History, as_named, edges_named, feed_of, history_of, recorded_edges


#: The estate's own warehouse, served from the catalog's own root with no registry record: the chart renders it as
#: `warehouse:lance_catalog` and refuses a `services.lineage.provenanceNamespace` outside it.
ESTATE_WAREHOUSE = "lance_catalog"


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.phase1,
    pytest.mark.skipif(not (scenario.LIVE and bus.NATS), reason=f"{scenario.REQUIRES}, and LANCE_E2E_NATS_URL (the bus)"),
]


@pytest.fixture(scope="module")
def history(byo_tenant: Tenant, byo_run: ScenarioRun) -> History:
    """Every version of every ref of the scenario's two tables and what its commit carries, read by the pipeline that
    owns them."""
    return history_of(byo_tenant.pipeline, (byo_run.bronze_table, byo_run.silver_table))


@pytest.fixture(scope="module")
def edges(byo_tenant: Tenant, byo_run: ScenarioRun, history: History) -> list[Edge]:
    """The edge list's rows naming either scenario table, read as the estate's owner."""
    return recorded_edges(byo_tenant.admin, (byo_run.bronze_table, byo_run.silver_table), expected=edges_named(history))


def test_each_version_is_a_pipeline_commit_carrying_the_run_it_named_and_who_committed_it(history: History, byo_tenant: Tenant, byo_run: ScenarioRun) -> None:
    """The pipeline made every commit and named each one's run. Its stock commits, bronze's seed and silver's append on
    `staging`, carry the run as the pipeline staged it, which `version/create` admits only under the caller's own
    principal; silver's create and insert carry the run the catalog stamped from the spec Context, beside the person
    who called its door."""
    assert history == as_named(byo_run.commits, author=byo_tenant.pipeline.principal)


def test_the_edge_list_holds_one_edge_for_each_input_a_version_names_and_no_other(history: History, edges: list[Edge]) -> None:
    """Compared as multisets, so an edge recorded twice is not an edge recorded once."""
    expected = edges_named(history)

    assert expected, "no version names an input, so there is no edge to hold the list to"
    assert Counter(edges) == Counter(expected)


def test_the_feed_is_the_events_table_of_the_estate_warehouses_provenance_namespace(byo_tenant: Tenant, byo_run: ScenarioRun) -> None:
    """Each row the feed served about the drive is a row of `lance_catalog$provenance$events`, read by the estate's owner
    through the stock client, and the release names that namespace as lineage's. A feed row's `seq` is its stable row
    id in that table plus one."""
    served = sorted(int(row["seq"]) for row in feed_of(byo_tenant.pipeline, byo_run))
    events = lance.dataset(namespace_client=byo_tenant.admin.namespace(), table_id=[ESTATE_WAREHOUSE, "provenance", "events"])

    rows = events.to_table(columns=["_rowid"], filter=f"_rowid IN ({', '.join(str(seq - 1) for seq in served)})")
    held = sorted(row_id + 1 for row_id in rows.column("_rowid").to_pylist())

    assert served, "the feed served no row about the drive, so there is nothing to find in the provenance namespace"
    assert (PROVENANCE_NAMESPACE, held) == (f"{ESTATE_WAREHOUSE}$provenance", served)
