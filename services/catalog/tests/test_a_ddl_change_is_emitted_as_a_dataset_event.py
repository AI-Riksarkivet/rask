"""A catalog DDL change goes on the wire as a ``DatasetEvent``; a data write stays a ``RunEvent``.

[[LIN-004]], ruled in `docs/adr/0070-four-owner-answers-and-the-reference-implementation-that.md` § D. `build_write_event` wrapped EVERY catalog operation in
a synthetic run, so a schema change minted a `(:Run)` that never executed and a `(:Job)` that never
ran — named per table per operation, so the phantom population grew with the table count. The `/jobs`
governance fold makes a Job's output set its access handle, which makes each one an access-control
object for an operation nobody performed.

ONE BUILDER, NOT TWO. The DDL form is the run form with its wrapper removed and its run facets moved
onto the dataset, because the facet CONTENT must not be able to differ between the two shapes. The
things that read it downstream are the authorizer (`author.sub`) and the notifications plane
(`author.sub`, `lance.project`, `lance.originator`), and all three look in the run slot today.
"""

from __future__ import annotations

import pytest

from catalog.core.lineage_emit import InputRef, build_write_event


DDL_OPERATIONS = (
    "create_table",
    "drop_index",
)
DATA_OPERATIONS = ("insert",)


def _event(operation: str, inputs: list[InputRef] | None = None) -> dict[str, object]:
    return build_write_event(
        table_id="alpha$bronze$images",
        namespace="alpha$bronze",
        author="alice",
        version=1,
        operation=operation,
        run_id="r1",
        event_time="2026-09-23T00:00:00+00:00",
        job_namespace="lance-catalog",
        project="acme",
        inputs=inputs,
    )


@pytest.mark.parametrize("operation", DDL_OPERATIONS)
def test_every_ddl_operation_is_emitted_as_a_dataset_event(operation: str) -> None:
    """A DDL op goes on the wire as a ``DatasetEvent``, carrying none of a run's members."""
    event = _event(operation)
    assert "dataset" in event, f"{operation} still emits a run-shaped event and will mint a phantom Job"
    assert "run" not in event and "job" not in event, f"{operation} carries members the spec forbids on a DatasetEvent"
    assert "eventType" not in event, f"{operation} carries an eventType, which a DatasetEvent does not define"


@pytest.mark.parametrize("operation", DATA_OPERATIONS)
def test_a_data_write_is_still_a_run_event(operation: str) -> None:
    """A write that rows went through IS a run — moving it would lose the job that performed it."""
    event = _event(operation)
    assert "run" in event and "job" in event, f"{operation} lost its run"
    assert event["eventType"] == "COMPLETE"


def test_a_DDL_change_that_DERIVED_the_table_keeps_its_run() -> None:
    """A `DatasetEvent` has only `dataset` — no `inputs` — and every `DERIVED_FROM` edge is built from a
    run's inputs. A rename passes its SOURCE so the destination is not an orphan with no history, so
    moving it would sever the chain the input exists to preserve. Not a phantom either: something did
    derive one table from another."""
    event = _event("rename_table", inputs=[InputRef("alpha$bronze", "alpha$bronze$old", None)])
    assert "run" in event and "job" in event, "a derived rename lost the run that carries its source"
    assert event["inputs"] == [{"namespace": "alpha$bronze", "name": "alpha$bronze$old"}]
