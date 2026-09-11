"""CONTRACT (LH-003, condition 1): a write on a BRANCH says so in its lineage event.

THE COLLISION, reproduced on the installed pylance rather than argued. A branch is a whole parallel
dataset under `tree/<name>/` with its OWN version sequence (`catalog/core/namespace.py::open_dataset`
states it, and `checkout_version((branch, None))` is how the catalog reaches one). Driven locally:

    main            v2  rows=3
    branch write    v3  rows=4   <- the branch
    later main write v3 rows=4   <- main, DIFFERENT contents

Same dataset, same version NUMBER, two different snapshots. The WROTE edge records only the number, so
the graph cannot tell them apart — and `LATEST_WRITE_VERSION` orders by `event_time DESC` alone, so the
most recent write WINS regardless of which ref it landed on.

`emit_measured_write` already holds the ref and already uses it to read the version off the right
dataset — its own docstring explains why ("reading a branch write back off main pins the WROTE edge to a
version that never carried the change") — and then drops it before the event. The read was fixed; the
event was not.
"""

from __future__ import annotations

import asyncio
from typing import Any

from catalog.core.lineage_emit import build_write_event, emit_write_event


class _Recording:
    """Structural double for `LineageEmitter` — the estate's fake-by-shape pattern."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def project_for(self, top_ns: str) -> str | None:
        return None

    async def emit_create(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)

    async def emit_write(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)


def _built_facet(*, branch: str | None) -> dict[str, Any]:
    """The `lance` run facet of the EVENT, built the way the emitter builds it.

    Asserted on the built event rather than on the emitter call, because the facet is composed inside
    `build_write_event` — a double that intercepts `emit_write` never sees it, which is what an earlier
    version of this test measured and why it proved nothing.
    """
    event = build_write_event(
        table_id="acme$silver$features",
        namespace="acme$silver",
        author="alice",
        version=3,
        operation="insert_into_table",
        run_id="r1",
        event_time="2026-09-11T00:00:00+00:00",
        job_namespace="lance-catalog",
        branch=branch,
    )
    return dict(event["run"]["facets"]["lance"])


def test_a_branch_write_NAMES_its_ref_in_the_event() -> None:
    """Without the ref, `version=3` is ambiguous between the branch and main — measured, both exist."""
    facet = _built_facet(branch="feat")
    assert facet.get("ref") == "feat", f"the event does not name the branch: {facet}"


def test_a_MAIN_write_carries_no_ref_rather_than_the_word_main() -> None:
    """ABSENT, not `"main"`. The graph's reads distinguish main by the property being missing, so a
    literal would make every historical main write (which has none) look like a different ref — and a
    consumer testing for presence would read "was I on a branch?" as yes for every write."""
    facet = _built_facet(branch=None)
    assert "ref" not in facet, f"a main write invented a ref: {facet}"


def test_the_trailer_FORWARDS_the_ref_it_already_holds() -> None:
    """`emit_measured_write` reads the version off the branch and then dropped the ref before the event.

    Pinned separately from the facet: the facet test proves the event CAN carry a ref, and this proves
    the write path actually hands one over. The defect was entirely in the second half — the read was
    already branch-aware and its docstring explains why.
    """
    rec = _Recording()
    asyncio.run(
        emit_write_event(
            rec,
            ["acme", "silver", "features"],
            delimiter="$",
            author="alice",
            version=3,
            operation="insert_into_table",
            authorization=None,
            branch="feat",
        )
    )
    assert rec.writes and rec.writes[0].get("branch") == "feat", f"the ref never reached the emitter: {rec.writes}"


def test_the_reads_that_answer_MAINS_version_exclude_branch_writes() -> None:
    """The consumer half: storing the ref buys nothing unless the reads use it.

    `LATEST_WRITE_VERSION` answers "what version is this table at" and `SCHEMA_LATEST` answers "what is
    its schema" — both ordered by `event_time DESC` alone, so the most recent write won whichever ref it
    landed on. `core/reconcile.py` then compares that number against MAIN's on-disk version, so a branch
    write could be classified as main drift and back-filled.

    `w.ref IS NULL` is the filter rather than `w.ref = 'main'`, and that is load-bearing for history:
    every write recorded before the ref existed carries no property, and those were all main writes.
    """
    from lineage.services import cypher as cy

    for name, statement in (("LATEST_WRITE_VERSION", cy.LATEST_WRITE_VERSION), ("SCHEMA_LATEST", cy.SCHEMA_LATEST)):
        assert "w.ref IS NULL" in statement, f"{name} still reports a branch write as main's: {statement}"
    assert "SET w.ref=$ref" in cy.SET_WROTE_REF, cy.SET_WROTE_REF
    # Its OWN statement, like the version beside it: AGE silently drops a $param in a SET fused to an
    # edge MERGE, so a ref written that way would be stored as null with no error.
    assert "MERGE" not in cy.SET_WROTE_REF, "the ref is fused to a MERGE — AGE would drop the parameter"
