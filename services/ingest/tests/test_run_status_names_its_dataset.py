"""A run's status says WHAT it wrote, so a reader can reach its lineage.

`RunStatusResponse` reported how a run went — status, units, committed version, whether it
published — and never what it produced. The record behind it has carried `project` and `dataset`
since it was written; only the response dropped them.

The cost was a dead end in the UI: `/compute/ingest/<run_id>` shows a COMPLETE run with a committed
version and cannot link to the dataset's lineage, because nothing on the wire says which dataset
that is. The sibling `IngestRunRow` (the runs BOARD) does carry `table`, derived from the lineage
event's outputs — so the estate had the field in one place and not the other, and the page that
most needs it was the one without.

Exposed rather than derived: the ingest service DISPATCHED this run and already holds the answer.
Re-deriving it from lineage would make the link depend on the provenance chain the link exists to
reach.
"""

from __future__ import annotations

from ingest.api import RunStatusResponse


def test_the_response_model_carries_project_and_dataset() -> None:
    """The two fields a lineage link needs."""
    fields = set(RunStatusResponse.model_fields)
    assert "project" in fields
    assert "dataset" in fields


def test_a_status_round_trips_its_dataset() -> None:
    """What the page reads is what the record held."""
    status = RunStatusResponse.model_validate(
        {
            "run_id": "b9b753c6-7809-5a6c-8505-8a29c2be02fd",
            "status": "COMPLETE",
            "units_total": 4,
            "units_done": 4,
            "errors": {},
            "committed_version": 2,
            "project": "acme",
            "dataset": "item3proof",
        }
    )
    assert status.project == "acme"
    assert status.dataset == "item3proof"
