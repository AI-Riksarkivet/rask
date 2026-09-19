"""This lane emits provenance, and the envelope is the shared package's ([[LIN-001]]).

Owner ruling 2026-09-18: the compute plane emits, through `lineage-kit`. `runners/dummy` proved the
shape, the FGA grant and the read-back; this is the second runner wired, and the only other sealed one
that can take a path dependency — the remaining six carry no `uv.lock`, so
`.docker/ray-runner.dockerfile`'s `uv sync --locked` cannot build them at all ([[CP-032]]).

WHAT THESE PIN IS THE LANE'S POLICY, not the envelope. Every `_schemaURL`, the producer URI and the
wire form belong to `lineage_kit` and are gated estate-wide by
`tests/unit/test_the_openlineage_envelope_has_one_vocabulary.py`. What is this runner's own statement
is: that its datasets are EXTERNAL store URIs rather than governed table ids, that a role literal is
not an address, and that a FAIL claims no rows.
"""

from __future__ import annotations

from typing import Any

import pytest

from runner.lineage import build_run_event, dataset_ref


_S3_IN = "s3://pages-bucket/batch/A0060198"
_S3_OUT = "s3://alto-bucket/out"


def _event(
    *,
    event_type: str = "COMPLETE",
    run_id: str = "htr-run-1",
    input_uri: str = _S3_IN,
    output_uri: str = _S3_OUT,
    pipeline: str = "htr",
    rows: int = 0,
    originator: str = "",
    project: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    """The WIRE form, through the official serializer — what the ingest would actually receive.

    Every parameter is named rather than `**kwargs` so the call sites below cannot drift from
    `build_run_event`'s signature without the type checker saying so.
    """
    return build_run_event(
        event_type=event_type,
        run_id=run_id,
        input_uri=input_uri,
        output_uri=output_uri,
        pipeline=pipeline,
        rows=rows,
        originator=originator,
        project=project,
        error=error,
    ).to_wire()


# --- the lane's datasets are OUTSIDE the governed estate ----------------------------------------- #


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("s3://bucket/some/prefix", ("s3://bucket", "some/prefix")),
        ("s3://bucket/trailing/", ("s3://bucket", "trailing")),
        # The whole bucket IS the dataset when the run reads all of it — never an empty name.
        ("s3://bucket", ("s3://bucket", "bucket")),
        ("/local/pages", ("file", "/local/pages")),
    ],
)
def test_a_store_uri_becomes_an_external_dataset_ref(uri: str, expected: tuple[str, str]) -> None:
    """`s3://bucket` and the bare `file` identifier are the two shapes
    `service_kit.lakehouse.naming.is_external_source_namespace` recognises as outside the estate."""
    assert dataset_ref(uri) == expected


def test_the_lane_never_mints_a_governed_table_id() -> None:
    """A bare `stage$name` would claim a catalog table this lane never wrote — and lineage's input
    check authorizes a governed namespace differently from an external one, so the forgery would be
    accepted rather than caught."""
    event = _event()

    for dataset in (*event["inputs"], *event["outputs"]):
        assert "$" not in dataset["namespace"], dataset


def test_the_output_records_where_the_run_actually_wrote() -> None:
    event = _event()

    assert (event["outputs"][0]["namespace"], event["outputs"][0]["name"]) == ("s3://alto-bucket", "out")


def test_the_input_edge_exists_at_all() -> None:
    """Without it the output lands in the graph as an orphan nobody can trace back to its pages."""
    event = _event()

    assert (event["inputs"][0]["namespace"], event["inputs"][0]["name"]) == ("s3://pages-bucket", "batch/A0060198")


# --- targeting: who hears about this run --------------------------------------------------------- #


def test_a_named_person_rides_the_originator_key() -> None:
    lance = _event(originator="user:alice", project="acme")["run"]["facets"]["lance"]

    assert (lance["originator"], lance["project"]) == ("user:alice", "acme")


@pytest.mark.parametrize("principal", ["ray", "service", "system", "data_eng", "*"])
def test_a_role_literal_is_dropped_rather_than_carried(principal: str) -> None:
    """A role literal is not an address: carried, it writes into an inbox actor literally named `ray`.
    That is the live defect in the medallion stage runners, and this lane must not reproduce it."""
    assert "originator" not in _event(originator=principal)["run"]["facets"]["lance"]


def test_the_author_key_is_never_set() -> None:
    """The ingest OVERWRITES it with the verified service sub, so setting it is silently discarded
    while looking handled — and honouring a producer-supplied author would let any producer file a row
    in a named person's inbox."""
    assert "author" not in _event(originator="user:alice")["run"]["facets"]["lance"]


# --- a FAIL claims nothing ------------------------------------------------------------------------ #


def test_a_completed_run_reports_its_row_count() -> None:
    assert _event(rows=42)["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == 42


def test_a_failed_run_carries_no_row_count() -> None:
    """A count on a FAIL makes a run that wrote nothing look like it produced data."""
    event = _event(event_type="FAIL", rows=42, error="boom")

    assert "outputStatistics" not in (event["outputs"][0].get("outputFacets") or {})


def test_a_failed_run_carries_the_standard_error_facet() -> None:
    event = _event(event_type="FAIL", error="boom")

    assert event["run"]["facets"]["errorMessage"]["message"] == "boom"


def test_a_long_error_is_bounded() -> None:
    """An unbounded message from a model stack's traceback is a row nobody can read and a payload the
    ingest may refuse outright."""
    assert len(_event(event_type="FAIL", error="x" * 5000)["run"]["facets"]["errorMessage"]["message"]) == 1000


# --- the run id ------------------------------------------------------------------------------------ #


def test_the_run_id_is_a_spec_legal_uuid() -> None:
    """The official serializer enforces it; the lane's readable seed is kept as the job name instead."""
    import uuid

    uuid.UUID(_event()["run"]["runId"])


def test_the_same_work_converges_on_the_same_run() -> None:
    """A rerun MERGEs onto the same graph node rather than adding a second one nobody asked for."""
    assert _event()["run"]["runId"] == _event()["run"]["runId"]


def test_different_work_is_a_different_run() -> None:
    """The control. Without it, a constant id would satisfy the case above and collapse every run in
    the estate onto one node."""
    assert _event()["run"]["runId"] != _event(run_id="htr-run-2")["run"]["runId"]


def test_the_job_name_carries_the_pipeline() -> None:
    """`htr` and `htrflow` are different work on the same lane; one job name for both makes the graph
    unable to say which produced a page."""
    assert _event(pipeline="htrflow")["job"]["name"] == "htr.htrflow"
