"""The consume-layer projection (R25b / R26): a run event becomes the JSONB a dataset carries.

These pin the properties the medallion's gold column rests on: the document is PROJECTED from the very
event that goes to the graph (so the two cannot disagree), the chain grows by one hop per stage from the
upstream's own document (so a terminal row reaches the root with no graph query), and a document never
claims the version of the commit that creates it.
"""

from __future__ import annotations

import json

import pytest
from lineage_kit.consume import LINEAGE_DOC_SCHEMA, DatasetRef, LineageDoc, LineageEdge, as_json_rows, parse_doc
from lineage_kit.schemas import PRODUCER, RunEvent, custom_facet


def _event(
    *,
    run_id: str = "11111111-1111-1111-1111-111111111111",
    operation: str = "aggregate_gold",
    author: str = "analyst",
    event_time: str = "2026-07-28T10:00:00+00:00",
    input_name: str = "silver$features",
    output_name: str = "gold$catalog",
    output_version: str | None = "4",
) -> RunEvent:
    output: dict[str, object] = {
        "namespace": "gold",
        "name": output_name,
        "facets": {"dataSource": {"name": "s3://lake/gold", "uri": "s3://lake/gold"}},
    }
    if output_version is not None:
        facets = output["facets"]
        assert isinstance(facets, dict)
        facets["version"] = {"datasetVersion": output_version}
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": event_time,
            "producer": PRODUCER,
            "run": {
                "runId": run_id,
                "facets": {
                    "lance": custom_facet(operation=operation, version=4),
                    "author": custom_facet(name=author, sub=author),
                },
            },
            "job": {"namespace": "lance-medallion", "name": operation},
            "inputs": [{"namespace": "silver", "name": input_name}],
            "outputs": [output],
        }
    )


def test_the_document_is_projected_from_the_event_not_authored_beside_it() -> None:
    # Everything a consumer reads off the JSONB comes from the event that also reaches the graph — which
    # is what makes "the two cannot disagree" a property of the code rather than a review promise.
    event = _event()
    doc = LineageDoc.from_run_event(event)

    assert doc.doc_schema == LINEAGE_DOC_SCHEMA
    assert doc.run_id == event.run.run_id
    assert (doc.job.namespace, doc.job.name) == ("lance-medallion", "aggregate_gold")
    assert doc.operation == "aggregate_gold"
    assert doc.author == "analyst"
    assert doc.event_time == "2026-07-28T10:00:00+00:00"
    assert doc.event_type == "COMPLETE"
    assert doc.producer == PRODUCER
    assert doc.is_consistent_with(event)


def test_the_document_never_claims_the_version_of_the_commit_that_creates_it() -> None:
    # The column is written INSIDE the write that mints the version, so lifting the event's output
    # version facet would publish a number that is a guess. The URI (known beforehand) is kept.
    doc = LineageDoc.from_run_event(_event(output_version="4"))
    assert doc.output.version is None
    assert doc.output.uri == "s3://lake/gold"


def test_measured_inputs_replace_the_events_bare_ones() -> None:
    # The emit records `{namespace, name}` inputs; the writer additionally KNOWS the upstream version and
    # URI it just read. Those are facts the event never claims, so adding them cannot contradict it.
    measured = DatasetRef(namespace="silver", name="silver$features", version="3", uri="s3://lake/silver")
    doc = LineageDoc.from_run_event(_event(), inputs=[measured])
    assert doc.inputs == [measured]
    assert doc.derived_from[0].upstream == measured


def test_the_chain_grows_one_hop_per_stage_and_reaches_the_root() -> None:
    silver = LineageDoc.from_run_event(
        _event(run_id="22222222-2222-2222-2222-222222222222", operation="embed_features", input_name="bronze$events", output_name="silver$features"),
        inputs=[DatasetRef(namespace="bronze", name="bronze$events", version="1")],
    )
    gold = LineageDoc.from_run_event(
        _event(),
        inputs=[DatasetRef(namespace="silver", name="silver$features", version="3")],
        parent_chain=LineageDoc.inherited_chain(silver.to_json()),
    )

    assert [(e.upstream.name, e.downstream.name) for e in gold.derived_from] == [
        ("silver$features", "gold$catalog"),
        ("bronze$events", "silver$features"),
    ]
    # …and every hop stays self-contained: the run that made it travels with it.
    assert [e.run_id for e in gold.derived_from] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    # `chain` is the flat mirror a single json_array_contains filter can answer "descended from X?" with.
    assert gold.chain == ["silver$features", "bronze$events"]


@pytest.mark.parametrize("raw", [None, "not json", '{"run_id": 5}'])
def test_an_unreadable_upstream_cell_degrades_the_chain_depth_never_its_truth(raw: str | None) -> None:
    # A pre-R26 upstream (no column) or a cell a foreign writer mangled must not fail the promotion: the
    # stage still records its OWN hop truthfully, it just cannot inherit what it could not read.
    assert LineageDoc.inherited_chain(raw) == []


def test_is_consistent_with_catches_a_document_that_drifted_from_its_event() -> None:
    event = _event()
    doc = LineageDoc.from_run_event(event)
    assert not doc.is_consistent_with(_event(event_time="2026-07-28T11:00:00+00:00"))
    assert not doc.is_consistent_with(_event(run_id="33333333-3333-3333-3333-333333333333"))
    assert not doc.is_consistent_with(_event(author="someone_else"))
    assert not doc.is_consistent_with(_event(input_name="silver$other"))


def test_an_event_with_no_output_cannot_become_a_document() -> None:
    # A FAIL run wrote no dataset, so there is nothing to stamp a column into — refuse rather than
    # fabricate an output ref that no data backs.
    event = _event()
    event.outputs = []
    with pytest.raises(ValueError, match="output dataset"):
        LineageDoc.from_run_event(event)


def test_the_json_round_trips_and_stamps_every_row() -> None:
    doc = LineageDoc.from_run_event(
        _event(),
        parent_chain=[
            LineageEdge(
                downstream=DatasetRef(namespace="silver", name="silver$features"),
                upstream=DatasetRef(namespace="bronze", name="bronze$events", version="1"),
                run_id="22222222-2222-2222-2222-222222222222",
                job={"namespace": "lance-medallion", "name": "embed_features"},
                event_time="2026-07-28T09:00:00+00:00",
            )
        ],
    )
    rows = as_json_rows(doc, 3)
    assert len(rows) == 3 and len(set(rows)) == 1  # one document, stamped on every row
    assert parse_doc(rows[0]) == doc
    # The wire form uses the aliased `schema` key and omits nulls — the shape a JSON filter addresses.
    wire = json.loads(rows[0])
    assert wire["schema"] == LINEAGE_DOC_SCHEMA
    assert "version" not in wire["output"]
