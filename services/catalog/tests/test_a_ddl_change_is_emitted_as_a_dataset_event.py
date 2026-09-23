"""A catalog DDL change goes on the wire as a ``DatasetEvent``; a data write stays a ``RunEvent``.

[[LIN-004]], ruled in `docs/DECISIONS.md` § D. `build_write_event` wrapped EVERY catalog operation in
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
    "create_table_version",
    "declare_table",
    "register_table",
    "drop_table",
    "deregister_table",
    "add_columns",
    "alter_columns",
    "drop_columns",
    "create_index",
    "drop_index",
    "update_schema_metadata",
    "rename_table",
)
DATA_OPERATIONS = ("insert", "delete", "merge_insert", "update", "compaction")


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
    """Parametrised over the SET, so a new DDL op joins this gate by being declared and nowhere else."""
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


def test_the_ddl_event_carries_the_verified_author_on_the_dataset() -> None:
    """The authorizer reads `author.sub`; without it every DDL change becomes unauthored and is refused."""
    dataset = _event("create_table")["dataset"]
    assert isinstance(dataset, dict)
    facets = dataset["facets"]
    assert isinstance(facets, dict)
    assert facets["author"]["sub"] == "alice"
    assert facets["author"]["_producer"] and facets["author"]["_schemaURL"]


def test_the_ddl_event_carries_the_operation_and_tenant_on_the_dataset() -> None:
    """`lance.operation` keys the CREATED edge and `lance.project` is watch targeting's only key."""
    dataset = _event("create_table")["dataset"]
    assert isinstance(dataset, dict)
    lance = dataset["facets"]["lance"]
    assert lance["operation"] == "create_table"
    assert lance["version"] == 1
    assert lance["project"] == "acme"


def test_the_ddl_event_keeps_the_standard_dataset_facets() -> None:
    """They already rode the output; losing them in the move would strip the graph of the schema and version."""
    dataset = _event("create_table")["dataset"]
    assert isinstance(dataset, dict)
    facets = dataset["facets"]
    assert facets["version"]["datasetVersion"] == "1"
    assert facets["lifecycleStateChange"]["lifecycleStateChange"] == "CREATE"
    assert facets["datasetType"]["datasetType"] == "TABLE"


def test_the_ddl_event_names_the_dataset_it_changed() -> None:
    """The authorizer gates on it and the graph merges on it, so the identity must survive the move."""
    dataset = _event("drop_table")["dataset"]
    assert isinstance(dataset, dict)
    assert dataset["namespace"] == "alpha$bronze"
    assert dataset["name"] == "alpha$bronze$images"


def test_a_DDL_change_that_DERIVED_the_table_keeps_its_run() -> None:
    """A `DatasetEvent` has only `dataset` — no `inputs` — and every `DERIVED_FROM` edge is built from a
    run's inputs. A rename passes its SOURCE so the destination is not an orphan with no history, so
    moving it would sever the chain the input exists to preserve. Not a phantom either: something did
    derive one table from another."""
    event = _event("rename_table", inputs=[InputRef("alpha$bronze", "alpha$bronze$old", None)])
    assert "run" in event and "job" in event, "a derived rename lost the run that carries its source"
    assert event["inputs"] == [{"namespace": "alpha$bronze", "name": "alpha$bronze$old"}]
