"""R26 — gold carries its lineage as a JSONB column, and the column is QUERYABLE, not just present.

Drives the real in-process cascade (producer → bronze→silver mover → silver→gold mover) against a temp
directory with real Lance, capturing every emitted OpenLineage event, then asserts the three things that
make the consume layer (R25b) real:

1. **The mover writes it.** Gold's schema carries ``lineage`` as Lance JSON, in the SAME commit as the
   data — and the promotion builds the JSON scalar index over it.
2. **It round-trips and answers filters IN PLACE.** Not "bytes landed": the dataset is re-opened and
   filtered with ``json_get_string`` / ``json_array_contains`` / ``json_array_length`` predicates, which
   only work because the value is stored JSONB (``pa.json_()``), and the rows come back.
3. **It cannot disagree with the graph.** The ``derived_from`` chain in the JSONB is compared edge for
   edge against the ``DERIVED_FROM`` edges the captured events would create in Apache AGE for the same
   runs, and the document is re-checked against the emitted event field by field.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import lance
import pytest
from dapr.aio.clients import DaprClient
from lineage_kit.consume import LineageDoc
from lineage_kit.schemas import RunEvent
from medallion.core.config import MedallionSettings
from medallion.schemas.htr import GOLD_CONTRACT_COLUMNS, LINEAGE_COLUMN
from medallion.services.produce import produce
from medallion.services.transform import handle_stage


_HOPS = [
    ("embed_features", "bronze", "bronze$events", "silver", "silver$features", "medallion.silver"),
    ("aggregate_gold", "silver", "silver$features", "gold", "gold$catalog", ""),
]


class _FakeDapr:
    """Captures every published event across the cascade (lineage emits + stage triggers)."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


def _run_cascade(tmp_path: Any) -> tuple[dict[str, str], _FakeDapr]:
    """Producer + both movers, compute ON — the same code path the deployed pods run."""
    uris = {ns: str(tmp_path / ns) for ns in ("bronze", "silver", "gold")}
    dapr = _FakeDapr()
    producer = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": uris["bronze"]})
    asyncio.run(produce(cast(DaprClient, dapr), producer, token="lineage-col"))
    for op, from_ns, from_ds, to_ns, to_ds, pub in _HOPS:
        settings = MedallionSettings.model_validate(
            {
                "compute_enabled": True,
                "from_uri": uris[from_ns],
                "to_uri": uris[to_ns],
                "from_namespace": from_ns,
                "from_dataset": from_ds,
                "to_namespace": to_ns,
                "to_dataset": to_ds,
                "operation": op,
                "author": "analyst" if to_ns == "gold" else "data_eng",
                "pub_topic": pub,
            }
        )
        assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "lineage-col"}})) == {"status": "SUCCESS"}
    return uris, dapr


def _lineage_events(dapr: _FakeDapr) -> list[dict[str, Any]]:
    return [p["data"] for p in dapr.published if p["topic"] == "lineage.events.v1"]


def _gold_doc(uris: dict[str, str]) -> LineageDoc:
    cell = lance.dataset(uris["gold"]).to_table(columns=[LINEAGE_COLUMN]).column(LINEAGE_COLUMN)[0].as_py()
    return LineageDoc.model_validate_json(cell)


# --------------------------------------------------------------------------- #
# 1. the mover writes it
# --------------------------------------------------------------------------- #


def test_the_promotion_writes_the_lineage_column_as_lance_json(tmp_path: Any) -> None:
    uris, _ = _run_cascade(tmp_path)
    gold = lance.dataset(uris["gold"])

    assert LINEAGE_COLUMN in gold.schema.names
    # Lance JSONB, not a string column — this is what the json_* scan functions dispatch on.
    assert gold.schema.field(LINEAGE_COLUMN).type.extension_name == "arrow.json"
    # Every row carries it: the consume unit is the ROW, so a projection that carries rows out carries
    # their provenance with them.
    cells = gold.to_table(columns=[LINEAGE_COLUMN]).column(LINEAGE_COLUMN).to_pylist()
    assert len(cells) == gold.count_rows()
    assert all(cell for cell in cells)


def test_the_lineage_column_is_part_of_the_gold_contract() -> None:
    # The exporter (P7c) is allowed to rely on it — a field dropped from gold is unrecoverable downstream.
    assert LINEAGE_COLUMN in GOLD_CONTRACT_COLUMNS


def test_the_promotion_indexes_the_run_id_path(tmp_path: Any) -> None:
    # R26's second half: a JSON scalar index over one JSONB path, rebuilt each run because the cascade's
    # mode="overwrite" drops indices. Without it every provenance filter is a full scan.
    uris, _ = _run_cascade(tmp_path)
    indices = lance.dataset(uris["gold"]).describe_indices()
    assert [(idx.type_url, list(idx.field_names)) for idx in indices] == [("/lance.index.pb.JsonIndexDetails", [LINEAGE_COLUMN])]


def test_the_lineage_column_is_re_stamped_not_inherited(tmp_path: Any) -> None:
    # Silver carries its OWN document (that is what lets gold's chain reach bronze). Gold must overwrite
    # it, never carry it forward — a gold row labelled with silver's run would be a provenance lie.
    uris, _ = _run_cascade(tmp_path)
    silver_doc = LineageDoc.model_validate_json(lance.dataset(uris["silver"]).to_table(columns=[LINEAGE_COLUMN]).column(LINEAGE_COLUMN)[0].as_py())
    gold_doc = _gold_doc(uris)
    assert lance.dataset(uris["gold"]).schema.names.count(LINEAGE_COLUMN) == 1
    assert gold_doc.operation == "aggregate_gold"
    assert silver_doc.operation == "embed_features"
    assert gold_doc.run_id != silver_doc.run_id


# --------------------------------------------------------------------------- #
# 2. the JSONB round-trips AND answers filters in place
# --------------------------------------------------------------------------- #


def test_the_document_round_trips_with_everything_an_external_consumer_needs(tmp_path: Any) -> None:
    uris, _ = _run_cascade(tmp_path)
    doc = _gold_doc(uris)

    assert doc.run_id  # the join key back to the lineage graph
    assert (doc.job.namespace, doc.job.name) == ("lance-medallion", "aggregate_gold")
    assert doc.author == "analyst"
    assert doc.operation == "aggregate_gold"
    assert doc.event_time
    assert doc.event_type == "COMPLETE"
    # the upstream, with the version READ and where it physically lives
    assert [(d.namespace, d.name, d.uri) for d in doc.inputs] == [("silver", "silver$features", uris["silver"])]
    assert doc.inputs[0].version == str(lance.dataset(uris["silver"]).version)
    assert (doc.output.namespace, doc.output.name, doc.output.uri) == ("gold", "gold$catalog", uris["gold"])


@pytest.mark.parametrize(
    "predicate",
    [
        "json_get_string(lineage, 'run_id') IS NOT NULL",
        "json_get_string(lineage, 'operation') = 'aggregate_gold'",
        "json_get_string(lineage, 'author') = 'analyst'",
        "json_get_string(json_get(lineage, 'job'), 'name') = 'aggregate_gold'",
        "json_array_contains(lineage, 'chain', 'bronze$events')",
        "json_array_length(lineage, 'derived_from') = 2",
        "json_exists(lineage, 'derived_from')",
    ],
)
def test_the_jsonb_answers_filters_in_place(tmp_path: Any, predicate: str) -> None:
    # Queryability is the point (R26): these predicates run INSIDE the Lance scan against stored JSONB.
    # A string column would fail every one of them — proving the type, not just the bytes.
    uris, _ = _run_cascade(tmp_path)
    gold = lance.dataset(uris["gold"])
    assert gold.to_table(columns=["id"], filter=predicate).num_rows == gold.count_rows()


def test_a_run_id_filter_selects_exactly_the_rows_that_run_produced(tmp_path: Any) -> None:
    # The indexed path: "give me every row produced by run X" is one predicate, no graph, no join.
    uris, dapr = _run_cascade(tmp_path)
    gold_event = _lineage_events(dapr)[-1]
    run_id = gold_event["run"]["runId"]
    gold = lance.dataset(uris["gold"])

    assert gold.to_table(columns=["id"], filter=f"json_get_string(lineage, 'run_id') = '{run_id}'").num_rows == gold.count_rows()
    assert gold.to_table(columns=["id"], filter="json_get_string(lineage, 'run_id') = 'no-such-run'").num_rows == 0


# --------------------------------------------------------------------------- #
# 3. the JSONB and the graph cannot disagree
# --------------------------------------------------------------------------- #


def test_the_documents_chain_matches_the_derived_from_edges_the_graph_gets(tmp_path: Any) -> None:
    """The chain in gold's JSONB == the ``DERIVED_FROM`` edges the same runs create in AGE.

    The lineage service turns each event into ``(:Dataset)-[:DERIVED_FROM]->(:Dataset)`` edges attributed
    to the run — so the graph's edge set for this cascade is derivable from the captured events. If the
    in-band chain and that set ever diverged, a consumer would get two different answers to one question.
    """
    uris, dapr = _run_cascade(tmp_path)
    graph_edges = {(event["run"]["runId"], inp["name"], event["outputs"][0]["name"]) for event in _lineage_events(dapr) for inp in event["inputs"]}
    doc_edges = {(edge.run_id, edge.upstream.name, edge.downstream.name) for edge in _gold_doc(uris).derived_from}

    assert doc_edges == graph_edges
    # …and it genuinely reaches the root governed tier (R23: bronze, never a raw table).
    assert _gold_doc(uris).derived_from[-1].upstream.name == "bronze$events"
    assert "bronze$events" in _gold_doc(uris).chain


def test_the_document_still_describes_the_event_that_was_published(tmp_path: Any) -> None:
    # Field-for-field: same run id, job, author, operation, instant, inputs, output. This is the pin that
    # the column is a PROJECTION of the emit rather than a second, independently-drifting record.
    uris, dapr = _run_cascade(tmp_path)
    emitted = RunEvent.model_validate(_lineage_events(dapr)[-1])
    assert _gold_doc(uris).is_consistent_with(emitted)


def test_the_document_names_the_same_instant_as_the_event(tmp_path: Any) -> None:
    # One eventTime per run, threaded into both. Without it the two records disagree on the only field a
    # consumer can order runs by time on.
    uris, dapr = _run_cascade(tmp_path)
    assert _gold_doc(uris).event_time == _lineage_events(dapr)[-1]["eventTime"]
