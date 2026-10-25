"""The client kit's OpenLineage builder passes the same spec-fidelity gate the lakehouse's emitters do.

Every event `client_kit.lancekit.openlineage` builds is validated against the vendored official core schema
(`tests/data/openlineage-2-0-2.json`, the 2-0-2 document `RUN_EVENT_SCHEMA_URL` names), and its column lineage is read
back through lineage's own `Dataset` model, which is how the lakehouse receives it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from client_kit.lancekit import openlineage as lancekit_ol
from lineage.models import ColumnUpstream, Dataset


_SPEC_PATH = Path(__file__).resolve().parents[3] / "tests" / "data" / "openlineage-2-0-2.json"
_SPEC: dict[str, Any] = json.loads(_SPEC_PATH.read_text())

_EVENT_TIME = "2026-07-26T09:00:00+00:00"


def _errors_against(definition: str, instance: object) -> list[str]:
    """Validation errors for ``instance`` against ``#/$defs/<definition>`` of the vendored core spec.

    ``format_checker`` is what makes ``runId``'s ``format: uuid`` and ``producer``'s ``format: uri``
    actually assert — jsonschema treats formats as annotations otherwise, which is precisely how a
    ``runId`` of ``"promote-1260245"`` passed unnoticed in the live feed.
    """
    root = {"$ref": f"#/$defs/{definition}", "$defs": _SPEC["$defs"], "$id": _SPEC["$id"]}
    validator = Draft202012Validator(root, format_checker=FormatChecker())
    return [f"{e.json_path}: {e.message}" for e in sorted(validator.iter_errors(instance), key=lambda e: e.json_path)]


def _assert_conforms(event: dict[str, Any]) -> None:
    """Validate against the definition this event's SHAPE claims to be.

    A DDL change is a `DatasetEvent` ([[LIN-004]]) and the spec's definition for one carries
    `"not": {"required": ["job", "run"]}` — so checking it against the right `$defs` entry is not a
    softer test, it is a stricter one: the schema itself refuses the members the phantom was made of.
    """
    definition = "DatasetEvent" if "dataset" in event else "RunEvent"
    errors = _errors_against(definition, event)
    assert not errors, f"as {definition}:\n" + "\n".join(errors)


def test_lancekit_annotation_event_conforms() -> None:
    result = lancekit_ol.WriteResult(
        version=4,
        row_count=12,
        size_bytes=0,
        fields=[{"name": "id", "type": "int64"}, {"name": "labels", "type": "json"}],
        column_map=[("id", "id", "IDENTITY"), ("labels", "labels", "IDENTITY")],
    )
    event = lancekit_ol.build_run_event(
        operation="MERGE_INSERT",
        job_namespace="media",
        job_name="annotate.merge_insert",
        inputs=[("media", "unit-7")],
        output_namespace="media",
        output_name="annotations",
        event_time=_EVENT_TIME,
        result=result,
        source_uri="s3://media/annotations",
        seed="annotate-annotations-unit-7-v4",
    )
    _assert_conforms(event)


def test_lancekit_column_lineage_uses_the_modern_transformations_array() -> None:
    # ASSERTS the slot a graph backend reads the kind from. The first cut put transformationType/
    # transformationSubtype on the InputField — valid JSON (InputField allows additional properties) but a slot
    # no consumer reads, so every forwarded edge arrived with an empty type and subtype. And lineage's own
    # authorizer must find the column's source dataset in the same facet, or it authorizes nothing.
    result = lancekit_ol.WriteResult(
        version=1,
        row_count=1,
        size_bytes=0,
        fields=[{"name": "labels", "type": "json"}],
        column_map=[("labels", "raw_labels", "TRANSFORMATION")],
    )
    event = lancekit_ol.build_run_event(
        operation="MERGE_INSERT",
        job_namespace="media",
        job_name="annotate.merge_insert",
        inputs=[("media", "unit-7")],
        output_namespace="media",
        output_name="annotations",
        event_time=_EVENT_TIME,
        result=result,
    )
    [source] = event["outputs"][0]["facets"]["columnLineage"]["fields"]["labels"]["inputFields"]
    assert [(t["type"], t["subtype"]) for t in source["transformations"]] == [("DIRECT", "TRANSFORMATION")]
    assert Dataset.model_validate(event["outputs"][0]).column_upstreams == [ColumnUpstream(namespace="media", name="unit-7")]
