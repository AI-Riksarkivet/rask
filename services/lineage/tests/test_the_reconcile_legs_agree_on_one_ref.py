"""CONTRACT (LH-009): every leg of the reconcile comparison means the SAME ref.

Reconciliation compares three things and acts destructively on the difference — it MERGEs a synthetic
run and a versioned WROTE edge when they disagree. The comparison is only meaningful if all three legs
name one ref:

    graph_version    `LATEST_WRITE_VERSION`        -> must exclude branch writes
    storage_version  `read_storage_version`        -> opens `lance.dataset(uri)`, which is MAIN only
    the repair       `backfill_write`              -> must record a MAIN write, i.e. stamp no ref

Before the ref existed, the first leg returned whichever write was most recent by `event_time` —
including one that landed on a BRANCH — and it was compared against main's on-disk version. A branch
write therefore read as main drift and triggered a back-fill on a 300 s tick.

Pinned as a set rather than one assertion each, because the failure mode is one leg becoming
branch-aware while the others do not: any single leg changing alone silently re-opens the same defect.
"""

from __future__ import annotations

import inspect

from lineage.core import reconcile as reconcile_mod
from lineage.services import cypher as cy
from lineage.services.repository import LineageRepository


def test_the_graph_leg_excludes_branch_writes() -> None:
    assert "w.ref IS NULL" in cy.LATEST_WRITE_VERSION, cy.LATEST_WRITE_VERSION


def test_the_storage_leg_reads_MAIN() -> None:
    """`lance.dataset(uri)` opens only main (`catalog/core/namespace.py::open_dataset` states it), so
    this leg is main by construction — asserted so a future `branch=` here has to face the other two."""
    source = inspect.getsource(reconcile_mod.read_storage_version)
    assert "lance.dataset(uri, storage_options=storage_options)" in source, source
    assert "branch" not in source, "the storage leg became branch-aware while the others did not"


def test_the_repair_leg_records_a_MAIN_write() -> None:
    """The back-fill recovers a write read off MAIN, so it must not stamp a ref — a ref here would make
    the repair invisible to the very query that found the drift."""
    source = inspect.getsource(LineageRepository.backfill_write)
    assert "SET_WROTE_REF" not in source, "the back-fill stamps a ref onto a write it read off main"
