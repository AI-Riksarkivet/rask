"""What a Phase 1 criterion reads to call the estate correct once a drive has ended (XC-091, XC-094, XC-095).

PROVENANCE. Lance holds the ground truth: each ref's version history, and the `rask.*` transaction properties each
commit carries (`read_transaction(v)`; `lance_docs/PROVENANCE.md` § Transaction properties, `catalog.core.run_context`).
Lineage records the table-level edge list from each commit's announcement: one row for each input a run names, from the
version it read to the version and ref it wrote (ADR 0158). `history_of` reads the first, `recorded_edges` the second,
and `edges_named` says what the second must hold, given the first.

A branch is its own version history (`lance_docs/file_format.md` § Branch and Tag Specification): its manifests and
transactions live under `tree/<branch>/`, and its history starts after the parent version `_refs/branches/<branch>.json`
records, which the branch lists as the clone it was cut from. So the walk takes the branches Lance lists from `_refs`,
never by listing `tree/`, and reads each one's versions past that parent version.

The edge list is read once lineage has acknowledged every message the bus held, so a missing or a duplicated edge is
lineage's decision, never an announcement still in flight; the read then waits out an announcement the catalog left in
its outbox for the relay. It is read the way its owner reads any table, by a stock client opening `provenance$edges`
through the namespace: the estate's owner owns the provenance namespace through the estate's default warehouse
(`catalog.services.provenance_namespace`).

EVENTS. Nothing a drive caused parks on the DLQ stream (`bus.messages_since("DLQ", ...)`), and the catalog's two
outboxes drain: `settled_outbox_depth` is the lineage outbox's depth as lineage's admin door reports it, and
`settled_control_outbox_depth` the control outbox's, listed with the store's own pair, each read once the settle window
allows its relay to drain what is staged.

Every wait is bounded by ``within``, the scenario's settle window unless a caller needs longer: a delivery its sidecar
retries waits out a 120 s step (`chart/templates/dapr-resiliency.yaml`), so a drill that takes a dependency down passes
a window of its own.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import bus
import byo_scenario as scenario
import lance
import requests
from byo_scenario import Identity
from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse import outbox


#: The namespace the catalog keeps lineage's tables in (`services.lineage.provenanceNamespace`).
PROVENANCE_NAMESPACE = os.environ.get("LANCE_E2E_PROVENANCE_NAMESPACE", "provenance")
#: The durable consumer through which lineage records what the catalog announces.
LINEAGE_DURABLE = "lineage-durable"
#: The catalog's control outbox (`LANCE_CONTROL_OUTBOX_URI`), and the store the outboxes live in with its own pair.
CONTROL_OUTBOX_URI = os.environ.get("LANCE_E2E_CONTROL_OUTBOX_URI", "s3://lance-catalog/_control_outbox")
STORE = os.environ.get("LANCE_E2E_S3", "")
STORE_KEY = os.environ.get("LANCE_E2E_S3_ACCESS_KEY", "")
STORE_SECRET = os.environ.get("LANCE_E2E_S3_SECRET_KEY", "")

#: Each version of a set of tables, as ``(table, branch, version)`` with ``None`` for main, and the `rask.*`
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


def rask_properties(properties: Mapping[str, str]) -> dict[str, Any]:
    """The `rask.*` entries of a commit's transaction properties, `rask.inputs` decoded."""
    rask: dict[str, Any] = {key: value for key, value in properties.items() if key.startswith("rask.")}
    if "rask.inputs" in rask:
        rask["rask.inputs"] = json.loads(rask["rask.inputs"])
    return rask


def _refs(main: lance.LanceDataset) -> list[tuple[str | None, lance.LanceDataset, int]]:
    """Each ref of a table as ``(branch, its head, the version its own history starts after)``: main from the start, then
    each branch Lance lists under `_refs/branches`, after the parent version it was cut at."""
    branches = main.branches.list()
    return [(None, main, 0), *((name, main.checkout_version((name, None)), int(info["parent_version"])) for name, info in branches.items())]


def history_of(reader: Identity, tables: Iterable[str]) -> History:
    """Every version of every ref of ``tables`` and what its commit carries, read by ``reader``."""
    found: History = {}
    for table in tables:
        main = lance.dataset(namespace_client=reader.namespace(), table_id=table.split("$"))
        for branch, head, parent_version in _refs(main):
            own_versions = [int(listed["version"]) for listed in head.versions() if int(listed["version"]) > parent_version]
            for version in own_versions:
                transaction = head.read_transaction(version)
                assert transaction is not None, f"{table} version {version} on {branch or 'main'} has no transaction to read"
                found[(table, branch, version)] = rask_properties(transaction.transaction_properties or {})
    return found


def edges_named(history: History) -> list[Edge]:
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


def _sql_list(names: Iterable[str]) -> str:
    """``names`` as a SQL list of string literals for a Lance filter."""
    return ", ".join("'" + name.replace("'", "''") + "'" for name in names)


def recorded_edges(owner: Identity, tables: Iterable[str], *, expected: Iterable[Edge], within: float = scenario.SETTLE_SECONDS) -> list[Edge]:
    """The edge list's rows naming any of ``tables``, read by the estate's ``owner``: first once lineage has acknowledged
    every message the bus holds, then again while ``expected`` names an edge they lack and ``within`` lasts, for an
    announcement the relay re-publishes from the catalog's outbox."""
    bus.wait_until_acked("LINEAGE", (LINEAGE_DURABLE,), bus.last_seq("LINEAGE"), within=within)
    named = _sql_list(tables)
    wanted = Counter(expected)
    deadline = time.monotonic() + within
    while True:
        edge_list = lance.dataset(namespace_client=owner.namespace(), table_id=[*PROVENANCE_NAMESPACE.split("$"), "edges"])
        rows = edge_list.to_table(columns=list(Edge.model_fields), filter=f"in_table IN ({named}) OR out_table IN ({named})").to_pylist()
        recorded = [Edge.model_validate(row) for row in rows]
        if wanted <= Counter(recorded) or time.monotonic() > deadline:
            return recorded
        time.sleep(2)


def _settled(depth: Callable[[], int], within: float) -> int:
    """``depth`` read until it is 0 or ``within`` is spent. A read the connection drops is read again: it is an
    idempotent GET, and a forward re-binding to a restarted pod drops the connections it has accepted."""

    def read() -> int | None:
        try:
            return depth()
        except requests.ConnectionError:
            return None

    deadline = time.monotonic() + within
    found = read()
    while found != 0 and time.monotonic() < deadline:
        time.sleep(3)
        found = read()
    assert found is not None, f"the depth could not be read within {within:.0f} s"
    return found


def settled_outbox_depth(reader: Identity, *, within: float = scenario.SETTLE_SECONDS) -> int:
    """The depth of the catalog's lineage outbox, once its relay has had ``within`` to drain it: the committed writes
    not yet confirmed delivered, as lineage's admin door counts them."""
    return _settled(lambda: int(scenario.ok(reader.http.get(f"{scenario.LINEAGE}/admin/dlq", params={"limit": 1}, timeout=30))["depth"]), within)


def settled_control_outbox_depth(*, within: float = scenario.SETTLE_SECONDS) -> int:
    """The depth of the catalog's control outbox, once its relay has had ``within`` to drain it: the control events
    staged and not yet confirmed published. No door reports it, so it is listed with the store's own pair."""
    assert STORE and STORE_KEY and STORE_SECRET, (
        "set LANCE_E2E_S3, LANCE_E2E_S3_ACCESS_KEY and LANCE_E2E_S3_SECRET_KEY: the control outbox is read from the store"
    )
    options = {
        "endpoint": STORE,
        "access_key_id": STORE_KEY,
        "secret_access_key": STORE_SECRET,
        "region": "us-east-1",
        "allow_http": str(STORE.startswith("http://")).lower(),
        "virtual_hosted_style_request": "false",
    }
    return _settled(lambda: outbox.backlog(CONTROL_OUTBOX_URI, options)[0], within)
