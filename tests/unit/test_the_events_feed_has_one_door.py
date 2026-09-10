"""The durable `/events` feed is written by the ingest transaction, and by nothing beside it.

the lakehouse register, row E4 (drained 2026-09-10; in git history).

The BEHAVIOUR — a failed feed write takes the graph write with it — is proven against real AGE by
`tests/e2e-py/test_lineage_e2e.py::test_a_failed_feed_write_takes_the_graph_write_with_it`, which needs
a live Apache AGE and skips without one. This file is the drift half, and it guards the shape rather
than the outcome, because the shape is what decayed: four call sites (HTTP ingest, the JetStream
consumer, the DLQ replay door, the reconcile relay) each did `ingest_event` and then a separate
`record_event_best_effort`, and each carried its own paragraph explaining that the second call must not
be forgotten. Four copies of "remember to also do X" is the defect stated in prose.

A second best-effort projection is easy to reintroduce and impossible to notice: it makes no test red,
it logs a WARNING nobody reads, and the store it desynchronises has no shared key with the graph to
reconcile against. So the rule is asserted directly — ONE writer inside the transaction, and exactly
one declared exception.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from lineage.services import repository as repo_mod


def _method_containing(source: str, call: str) -> set[str]:
    """Every function/method in `source` whose body contains a call to `self.<call>`."""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == call:
                found.add(node.name)
    return found


def _references(source: str, name: str) -> bool:
    """True when `source` imports or calls `name` — an occurrence in a docstring or comment is not one."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
        if isinstance(node, ast.ImportFrom | ast.Import) and any(a.name == name or a.asname == name for a in node.names):
            return True
    return False


def _repository_source() -> str:
    return Path(inspect.getfile(repo_mod)).read_text(encoding="utf-8")


def test_the_ingest_transaction_writes_the_feed_row() -> None:
    """The headline: the graph write and the feed row are one unit of work."""
    writers = _method_containing(_repository_source(), "_insert_feed_row")
    assert "ingest_event" in writers, (
        f"`ingest_event` no longer writes the feed row (writers: {sorted(writers)}). Every ingest path goes "
        "through it, so an event would reach the AGE graph and never reach /events — the feed services/"
        "notifications walks as the estate's catch-up path after an outage"
    )


def test_the_backfill_repair_writes_its_feed_row_in_its_own_transaction_too() -> None:
    """`backfill_write` is the second graph writer, and it had the same split: four Cypher statements
    made atomic and the feed row left outside them. A repair that reaches the graph and not /events is
    one the audit surface cannot show, while the sweep that made it reports success."""
    writers = _method_containing(_repository_source(), "_insert_feed_row")
    assert "backfill_write" in writers, f"`backfill_write` no longer writes its feed row inside its transaction (writers: {sorted(writers)})"


def test_no_production_method_writes_the_feed_on_its_own_connection() -> None:
    """`record_event` opens its own connection, so any caller of it is a write that can succeed or fail
    independently of the graph. It is kept only as a seeding primitive for the feed's own dedup tests."""
    callers = sorted(_method_containing(_repository_source(), "record_event") - {"record_event"})
    assert not callers, (
        f"these methods write the feed on their own connection: {callers}. A feed write outside the graph "
        "transaction can fail while the graph write succeeds, and nothing afterwards can tell — the two "
        "stores share no key to reconcile on. Pass the transaction's connection to `_insert_feed_row`"
    )


def test_no_module_still_projects_the_feed_beside_an_ingest() -> None:
    """The four duplicated call sites, gated at the estate level rather than one file at a time."""
    root = Path(inspect.getfile(repo_mod)).parents[2]
    # AST, not a text scan: the name is discussed in prose in this very estate (including in the
    # docstring that records why it went), and prose is not a call site.
    offenders = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py") if _references(p.read_text(encoding="utf-8"), "record_event_best_effort"))
    assert not offenders, (
        f"a best-effort feed projection is back in {offenders}. It swallows its own failure, so the event "
        "lands in the graph, never reaches /events, and the only trace is a WARNING whose `extra=` fields "
        "`kubectl logs` drops"
    )
