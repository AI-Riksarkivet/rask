"""Unit tests for the lineage service's pure logic (no database).

Covers OpenLineage event parsing (camelCase aliases + author facet) and the AGE
SQL/result helpers — the parts we own, deterministic and infra-free.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql

from lineage.core.age import _parse, _sql
from lineage.models import OutputStatistics, RunEvent


_SAMPLE = Path(__file__).resolve().parent.parent.parent / "services" / "lineage" / "src" / "lineage" / "sample_events.json"


def test_run_event_parses_openlineage_camelcase() -> None:
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-06-20T09:00:00Z",
            "run": {"runId": "r1", "facets": {"author": {"name": "alice"}}},
            "job": {"namespace": "lance-jobs", "name": "ingest"},
            "inputs": [{"namespace": "source", "name": "raw_images"}],
            "outputs": [{"namespace": "bronze", "name": "bronze$images"}],
        }
    )
    assert event.event_type == "COMPLETE"
    assert event.run.run_id == "r1"
    assert event.author == "alice"
    assert event.inputs[0].name == "raw_images"
    assert event.outputs[0].name == "bronze$images"


def test_author_absent_is_none() -> None:
    event = RunEvent.model_validate(
        {
            "eventType": "START",
            "eventTime": "t",
            "run": {"runId": "r2"},
            "job": {"namespace": "j", "name": "n"},
        }
    )
    assert event.author is None


def test_the_run_hierarchy_consumer_side_stays_deleted() -> None:
    """F-LIN-12, second half (owner ruling): DELETE the ``parent``-facet consumer side.

    ``parent_run_id``/``root_run_id`` were speculative — nothing in the estate emits the ``parent``
    run facet and no production code read either property; only their own tests did. The owner ruled
    the consumer-before-producer argument in their docstrings void. (``lineage_kit.context``'s
    ``root_run_id`` is a different, live symbol on the emitter side and is unaffected.)
    """
    assert not hasattr(RunEvent, "parent_run_id")
    assert not hasattr(RunEvent, "root_run_id")


def _output_with_stats(facets: dict[str, Any]) -> Any:
    from lineage.models import Dataset

    return Dataset.model_validate({"namespace": "bronze", "name": "bronze$events", "facets": facets})


def test_statistics_parses_output_statistics_facet() -> None:
    """The runtime-measured (row_count, size_bytes) is read from the standard outputStatistics facet."""
    ds = _output_with_stats({"outputStatistics": {"rowCount": 8, "size": 132}})
    assert ds.statistics == OutputStatistics(row_count=8, size_bytes=132)


def test_statistics_absent_when_no_facet() -> None:
    # A dummy emit (no compute) carries no outputStatistics facet → None, so no stats land on the edge.
    assert _output_with_stats({"version": {"datasetVersion": "1"}}).statistics is None


def test_statistics_partial_facet_reports_none_for_the_absent_half() -> None:
    # Both fields are spec-optional; a facet with only one records what was measured and reports None for
    # the other — never a fabricated -1 that producers() would serve as a real measurement.
    assert _output_with_stats({"outputStatistics": {"rowCount": 5}}).statistics == OutputStatistics(row_count=5)
    assert _output_with_stats({"outputStatistics": {"size": 64}}).statistics == OutputStatistics(size_bytes=64)


def test_quality_assertions_parses_facet() -> None:
    """The validator gate's assertions are read from the standard dataQualityAssertions facet."""
    ds = _output_with_stats(
        {
            "dataQualityAssertions": {
                "assertions": [
                    {"assertion": "row_count_positive", "success": True},
                    {"assertion": "not_null", "success": False, "column": "id"},
                ]
            }
        }
    )
    assert ds.quality_assertions == [
        {"assertion": "row_count_positive", "success": True},
        {"assertion": "not_null", "success": False, "column": "id"},
    ]


def test_quality_assertions_absent_when_no_facet() -> None:
    assert _output_with_stats({"version": {"datasetVersion": "1"}}).quality_assertions == []


def test_job_source_location_parses_facet() -> None:
    """The job's code location is read from the standard sourceCodeLocation facet (type+url required)."""
    from lineage.models import Job

    job = Job.model_validate(
        {
            "namespace": "lance-medallion",
            "name": "embed_features",
            "facets": {
                "sourceCodeLocation": {
                    "type": "git",
                    "url": "https://github.com/Borg93/lance-ns",
                    "path": "services/medallion",
                    "branch": "main",
                }
            },
        }
    )
    assert job.source_location == {
        "type": "git",
        "url": "https://github.com/Borg93/lance-ns",
        "path": "services/medallion",
        "branch": "main",
    }


def test_job_source_location_absent_is_none() -> None:
    from lineage.models import Job

    assert Job.model_validate({"namespace": "j", "name": "n"}).source_location is None


_JOBS = ["ingest_events", "embed_features", "embed_features", "caption_features", "aggregate_gold"]


def test_sample_events_all_valid() -> None:
    events = [RunEvent.model_validate(e) for e in json.loads(_SAMPLE.read_text())]
    assert [e.job.name for e in events] == _JOBS


def test_emitter_output_parses_in_service_model() -> None:
    """Events from the real OpenLineage client must round-trip into our Pydantic model."""
    from lineage.seed import events_as_dicts

    events = [RunEvent.model_validate(e) for e in events_as_dicts()]
    assert [e.job.name for e in events] == _JOBS
    assert events[0].author == "alice"  # custom AuthorRunFacet read through
    assert events[0].outputs[0].name == "bronze$events"
    # events[1] is the FAILED embed attempt, events[2] the successful retry.
    assert events[1].author == "data_eng" and events[1].event_type.upper() == "FAIL"
    assert events[2].author == "data_eng" and events[2].is_success


def test_silver_refinement_records_two_versions() -> None:
    """The two successful passes over silver produce versions 1 then 2 ('run against silver again')."""
    from lineage.seed import events_as_dicts

    events = [RunEvent.model_validate(e) for e in events_as_dicts()]
    # events[2] embed wrote silver v1; events[3] caption refined it in place -> v2.
    assert events[2].output_version("silver$features") == "1"
    assert events[3].output_version("silver$features") == "2"
    # the refine reads AND writes silver (in-place) — a version bump, not a new lineage edge.
    assert events[3].inputs[0].name == events[3].outputs[0].name == "silver$features"


def test_failed_run_exposes_producer_error_and_standard_dataset_facets() -> None:
    """The failed embed carries producer + errorMessage; outputs carry dataSource + tags facets."""
    from lineage.seed import events_as_dicts

    failed = RunEvent.model_validate(events_as_dicts()[1])
    assert failed.event_type.upper() == "FAIL" and not failed.is_success
    assert failed.producer and failed.producer.startswith("https://")
    assert failed.error_message and "OOM" in failed.error_message
    out = failed.outputs[0]
    assert out.name == "silver$features"
    assert out.source_uri == "s3://lakehouse/silver/features"  # standard dataSource facet
    assert "layer=silver" in out.tags  # standard tags facet


def test_dataset_exposes_schema_fields_from_standard_facet() -> None:
    """#24: the standard SchemaDatasetFacet is now surfaced (was discarded) for per-version persistence."""
    from lineage.seed import events_as_dicts

    out = RunEvent.model_validate(events_as_dicts()[3]).outputs[0]  # silver v2: 4 columns
    by_name = {f.name: f.type for f in out.fields}
    assert by_name == {
        "id": "int",
        "payload_src": "string",
        "embedding": "array<float>",
        "caption": "string",
    }


def test_dataset_exposes_column_edges_from_columnlineage_facet() -> None:
    """#24: the standard columnLineage facet is surfaced as flattened input→output column edges."""
    from lineage.seed import events_as_dicts

    out = RunEvent.model_validate(events_as_dicts()[2]).outputs[0]  # embed success: silver <- bronze
    by_out = {e.out_field: (e.name, e.field, e.type, e.subtype) for e in out.column_edges}
    assert by_out["embedding"] == ("bronze$events", "payload", "DIRECT", "TRANSFORMATION")  # real change
    assert by_out["id"] == ("bronze$events", "id", "DIRECT", "IDENTITY")  # pass-through
    # event 3 = caption: a SAME-dataset column edge (caption <- embedding), the in-place-refinement flow.
    cap = RunEvent.model_validate(events_as_dicts()[3]).outputs[0]
    assert any(e.out_field == "caption" and e.name == "silver$features" and e.field == "embedding" for e in cap.column_edges)


def test_author_falls_back_to_standard_ownership_facet() -> None:
    """With no custom author facet, the run author comes from the standard ownership job facet."""
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "t",
            "run": {"runId": "r"},
            "job": {
                "namespace": "ray-jobs",
                "name": "j",
                "facets": {"ownership": {"owners": [{"name": "carol", "type": "user"}]}},
            },
        }
    )
    assert event.author == "carol"


def test_sql_inlines_validated_graph_and_adds_params_arg() -> None:
    with_params = _sql("lineage", "MATCH (d) RETURN d", with_params=True, columns=1).as_string()
    assert "cypher('lineage'" in with_params
    assert "$$ MATCH (d) RETURN d $$" in with_params  # AGE requires dollar-quoting
    assert "%s" in with_params  # params bind slot present
    no_params = _sql("lineage", "RETURN 1", with_params=False, columns=2).as_string()
    assert "%s" not in no_params  # omitted when there are no params
    assert '"c0" agtype, "c1" agtype' in no_params


def test_sql_rejects_non_identifier_graph() -> None:
    with pytest.raises(ValueError, match="invalid graph name"):
        _sql("lineage; DROP TABLE x", "RETURN 1", with_params=False, columns=1)


class _Tx:
    async def __aenter__(self) -> _Tx:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _Conn:
    def transaction(self) -> _Tx:
        return _Tx()


class _PoolCM:
    async def __aenter__(self) -> _Conn:
        return _Conn()

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakePool:
    """Just enough of an AsyncConnectionPool to exercise ingest_event without a database."""

    def connection(self) -> _PoolCM:
        return _PoolCM()


def _capture_ingest(monkeypatch: pytest.MonkeyPatch, event_index: int) -> list[tuple[str, dict[str, object]]]:
    """Ingest one seed event against a fake pool, capturing the Cypher calls it issues."""
    import lineage.services.repository as repo_mod
    from lineage.seed import events_as_dicts

    calls: list[tuple[str, dict[str, object]]] = []

    async def _capture(_conn: object, _graph: str, query: str, params: dict[str, object], *, columns: int = 1) -> list[list[object]]:
        del columns  # signature parity with the real helper (#49 reads governance during ingest)
        calls.append((query, params))
        return []  # a read (the tag-union governance lookup) sees "no node yet" → union starts empty

    monkeypatch.setattr(repo_mod, "run_cypher", _capture)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    asyncio.run(repo.ingest_event(RunEvent.model_validate(events_as_dicts()[event_index])))
    return calls


def test_ingest_records_version_and_skips_self_derived_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-place silver refinement records version=2 on WROTE and emits NO self-DERIVED_FROM."""
    calls = _capture_ingest(monkeypatch, 3)  # silver -> silver (add caption), v2

    # version is written in its own MATCH...SET statement (AGE param-binding quirk on MERGE+SET).
    set_version = [p for q, p in calls if "SET w.version" in q]
    assert any(p.get("name") == "silver$features" and p.get("ver") == "2" for p in set_version)
    # #24: the per-version column schema is persisted onto the same WROTE edge, as a JSON string.
    set_schema = [p for q, p in calls if "SET w.schema" in q]
    assert set_schema and set_schema[0]["name"] == "silver$features"
    assert {f["name"] for f in json.loads(cast(str, set_schema[0]["schema"]))} == {
        "id",
        "payload_src",
        "embedding",
        "caption",
    }
    # in-place transform: read + write the same table, but NO dataset self-DERIVED_FROM edge. (The column
    # layer DOES keep the same-dataset caption<-embedding edge — asserted in test_ingest_keeps_same_*.)
    assert [p for q, p in calls if "[:DERIVED_FROM]" in q] == []
    # the standard dataSource + tags facets are persisted onto the dataset node.
    assert any("source_uri" in q for q, _ in calls)
    assert any("SET d.tags" in q for q, _ in calls)  # the WRITE, not the #49 governance read


def test_ingest_records_job_source_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """An event whose job carries a sourceCodeLocation facet stores it (as JSON) on the (:Job) node."""
    import lineage.services.repository as repo_mod

    calls: list[tuple[str, dict[str, object]]] = []

    async def _capture(_conn: object, _graph: str, query: str, params: dict[str, object], *, columns: int = 1) -> list[list[object]]:
        del columns  # signature parity with the real helper (#49 reads governance during ingest)
        calls.append((query, params))
        return []  # a read (the tag-union governance lookup) sees "no node yet" → union starts empty

    monkeypatch.setattr(repo_mod, "run_cypher", _capture)
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-01T09:00:00Z",
            "run": {"runId": "embed-1"},
            "job": {
                "namespace": "lance-medallion",
                "name": "embed_features",
                "facets": {
                    "sourceCodeLocation": {
                        "type": "git",
                        "url": "https://github.com/Borg93/lance-ns",
                        "path": "services/medallion",
                    }
                },
            },
            "inputs": [],
            "outputs": [{"namespace": "silver", "name": "silver$features"}],
        }
    )
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    asyncio.run(repo.ingest_event(event))

    set_src = [p for q, p in calls if "SET j.source_location" in q]
    assert set_src and set_src[0]["nm"] == "embed_features"
    assert json.loads(cast(str, set_src[0]["src"]))["path"] == "services/medallion"


def test_ingest_records_output_statistics_on_wrote_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful write carrying an outputStatistics facet sets row_count + size_bytes on its WROTE edge."""
    import lineage.services.repository as repo_mod

    calls: list[tuple[str, dict[str, object]]] = []

    async def _capture(_conn: object, _graph: str, query: str, params: dict[str, object], *, columns: int = 1) -> list[list[object]]:
        del columns  # signature parity with the real helper (#49 reads governance during ingest)
        calls.append((query, params))
        return []  # a read (the tag-union governance lookup) sees "no node yet" → union starts empty

    monkeypatch.setattr(repo_mod, "run_cypher", _capture)
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-06-30T09:00:00Z",
            "run": {"runId": "ingest_events-abc"},
            "job": {"namespace": "lance-jobs", "name": "ingest_events"},
            "inputs": [],
            "outputs": [
                {
                    "namespace": "bronze",
                    "name": "bronze$events",
                    "facets": {
                        "version": {"datasetVersion": "3"},
                        "outputStatistics": {"rowCount": 8, "size": 132},
                    },
                }
            ],
        }
    )
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    asyncio.run(repo.ingest_event(event))

    set_stats = [p for q, p in calls if "SET w.row_count" in q]
    assert set_stats and set_stats[0]["name"] == "bronze$events"
    assert set_stats[0]["rows"] == 8 and set_stats[0]["size"] == 132


def test_ingest_records_quality_on_wrote_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed quality assertion is recorded on the WROTE edge: passed=False + the assertions JSON."""
    import lineage.services.repository as repo_mod

    calls: list[tuple[str, dict[str, object]]] = []

    async def _capture(_conn: object, _graph: str, query: str, params: dict[str, object], *, columns: int = 1) -> list[list[object]]:
        del columns  # signature parity with the real helper (#49 reads governance during ingest)
        calls.append((query, params))
        return []  # a read (the tag-union governance lookup) sees "no node yet" → union starts empty

    monkeypatch.setattr(repo_mod, "run_cypher", _capture)
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-01T09:00:00Z",
            "run": {"runId": "aggregate_gold-x"},
            "job": {"namespace": "lance-jobs", "name": "aggregate_gold"},
            "inputs": [],
            "outputs": [
                {
                    "namespace": "gold",
                    "name": "gold$ml",
                    "facets": {
                        "version": {"datasetVersion": "2"},
                        "dataQualityAssertions": {
                            "assertions": [
                                {"assertion": "row_count_positive", "success": True},
                                {"assertion": "not_null", "success": False, "column": "id"},
                            ]
                        },
                    },
                }
            ],
        }
    )
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    asyncio.run(repo.ingest_event(event))

    set_q = [p for q, p in calls if "SET w.quality_passed" in q]
    assert set_q and set_q[0]["name"] == "gold$ml"
    assert set_q[0]["passed"] is False  # one assertion failed → the batch was gate-blocked
    assert json.loads(cast(str, set_q[0]["assertions"]))[1]["column"] == "id"


def test_failed_run_records_error_but_no_version_or_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed run is recorded (run + error + WROTE) but asserts NO version and NO derivation."""
    calls = _capture_ingest(monkeypatch, 1)  # the FAILED embed attempt

    run_merges = [p for q, p in calls if "MERGE (r:Run" in q]
    assert run_merges and run_merges[0].get("err")  # errorMessage stored on the run
    assert any("MERGE (r)-[:WROTE]" in q for q, _ in calls)  # the attempt is recorded as a WROTE edge
    assert [p for q, p in calls if "SET w.version" in q] == []  # but no produced version
    # a failed run produced no data, so it must not assert lineage.
    assert [p for q, p in calls if "DERIVED_FROM" in q] == []
    # #24: nor any column lineage (the columnLineage facet is present on the failed output but ignored).
    assert [p for q, p in calls if "DERIVED_FROM_COLUMN" in q] == []


def test_ingest_records_column_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    """#24: a successful run seeds typed Column nodes + DERIVED_FROM_COLUMN edges from columnLineage."""
    calls = _capture_ingest(monkeypatch, 2)  # embed success: silver <- bronze

    # the full column set is seeded with Arrow types from the schema facet
    typed = [p for q, p in calls if "SET c.namespace=$ns, c.type=$type" in q]
    assert {p["fld"] for p in typed} >= {"id", "payload_src", "embedding"}
    # a field-to-field edge embedding <- bronze.payload (MERGE) ...
    col_merges = [p for q, p in calls if "MERGE (o)-[:DERIVED_FROM_COLUMN]" in q]
    assert any(p["ofld"] == "embedding" and p["ids"] == "bronze$events" and p["ifld"] == "payload" for p in col_merges)
    # ... with its transformation kind persisted in a SEPARATE SET (the AGE post-MERGE-edge quirk)
    emb = next(p for q, p in calls if "SET e.transformation_type" in q and p["ofld"] == "embedding")
    assert emb["tt"] == "DIRECT" and emb["st"] == "TRANSFORMATION"
    # the INPUT-column stub uses the namespace-only MERGE (never sets type) — the type-clobber guard.
    # The discriminating substring excludes _MERGE_COLUMN_TYPED, which has ", c.type=$type" before RETURN.
    stub = [p for q, p in calls if "SET c.namespace=$ns RETURN" in q]
    assert any(p["fld"] == "payload" for p in stub)  # bronze.payload was stubbed (not yet typed)
    assert all("type" not in p for p in stub)  # the stub never carries a type to clobber with


def _masked_event() -> dict[str, Any]:
    """A run that derives a hashed column from a sensitive one — a masking column edge (#24 governance)."""
    return {
        "eventType": "COMPLETE",
        "eventTime": "t",
        "run": {"runId": "mask-run-1"},
        "job": {"namespace": "ray-jobs", "name": "redact"},
        "inputs": [{"namespace": "bronze", "name": "bronze$pii"}],
        "outputs": [
            {
                "namespace": "silver",
                "name": "silver$pii",
                "facets": {
                    "version": {"datasetVersion": "1"},
                    "schema": {"fields": [{"name": "ssn_hash", "type": "string"}]},
                    "columnLineage": {
                        "fields": {
                            "ssn_hash": {
                                "inputFields": [
                                    {
                                        "namespace": "bronze",
                                        "name": "bronze$pii",
                                        "field": "ssn",
                                        # masking rides a NON-FIRST transformation — must not be lost.
                                        "transformations": [
                                            {"type": "DIRECT", "subtype": "TRANSFORMATION"},
                                            {"type": "MASKED", "masking": True},
                                        ],
                                    }
                                ]
                            }
                        }
                    },
                },
            }
        ],
    }


def test_column_edges_masking_uses_any_across_transformations() -> None:
    """#24 audit: masking is true if ANY transformation masks (not just the first); kind from the first."""
    out = RunEvent.model_validate(_masked_event()).outputs[0]
    edge = next(e for e in out.column_edges if e.out_field == "ssn_hash")
    assert edge.masking is True  # the masking bit on the non-first transformation is preserved
    assert (edge.type, edge.subtype) == ("DIRECT", "TRANSFORMATION")  # kind still from transforms[0]


def test_column_edges_deprecated_fields_level_fallback() -> None:
    """#24: an older/Marquez-style producer using the deprecated Fields-level transformation still parses,
    and a deprecated MASKED type maps to the masking governance bit."""
    from lineage.models import Dataset

    ds = Dataset(
        namespace="silver",
        name="silver$x",
        facets={
            "columnLineage": {
                "fields": {
                    "x": {
                        "transformationType": "MASKED",
                        "transformationDescription": "redacted",
                        "inputFields": [{"namespace": "n", "name": "src$t", "field": "y"}],  # no transformations[]
                    }
                }
            }
        },
    )
    edge = ds.column_edges[0]
    assert edge.type == "MASKED" and edge.description == "redacted" and edge.masking is True


def test_ingest_persists_masking_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    """#24: the masking bit reaches the DERIVED_FROM_COLUMN edge's SET (the per-edge governance signal)."""
    import lineage.services.repository as repo_mod

    calls: list[tuple[str, dict[str, object]]] = []

    async def _capture(_conn: object, _graph: str, query: str, params: dict[str, object], *, columns: int = 1) -> list[list[object]]:
        del columns  # signature parity with the real helper (#49 reads governance during ingest)
        calls.append((query, params))
        return []  # a read (the tag-union governance lookup) sees "no node yet" → union starts empty

    monkeypatch.setattr(repo_mod, "run_cypher", _capture)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    asyncio.run(repo.ingest_event(RunEvent.model_validate(_masked_event())))
    set_edge = next(p for q, p in calls if "SET e.transformation_type" in q)
    assert set_edge["mask"] is True  # masking is written, not dropped


def test_ingest_keeps_same_dataset_column_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """#24: caption <- embedding WITHIN silver (in-place refinement) IS recorded — the column layer keeps
    same-dataset cross-field edges, unlike the dataset-level self-derivation skip."""
    calls = _capture_ingest(monkeypatch, 3)  # the caption pass: silver -> silver
    col_merges = [p for q, p in calls if "MERGE (o)-[:DERIVED_FROM_COLUMN]" in q]
    assert any(p["ods"] == "silver$features" and p["ofld"] == "caption" and p["ids"] == "silver$features" and p["ifld"] == "embedding" for p in col_merges)


def test_parse_handles_scalars_vertices_and_null() -> None:
    assert _parse('"bronze$images"') == "bronze$images"
    assert _parse("3") == 3
    assert _parse(None) is None
    vertex = _parse('{"id":1,"label":"Dataset","properties":{"name":"x"}}::vertex')
    assert vertex["label"] == "Dataset"
    assert vertex["properties"]["name"] == "x"


def test_prune_runs_batches_deletes_under_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """§4: the retention prune deletes in LIMIT-bounded batches (one txn each) — a single all-or-nothing
    DETACH DELETE over a big backlog would exceed the pool's statement_timeout, roll back entirely, and
    retry the identical oversized delete forever (retention would never converge)."""
    import lineage.services.repository as repo_mod

    calls: list[str] = []

    async def _fake(_conn: object, _graph: str, query: str, params: dict[str, object] | None = None, *, columns: int = 1) -> list[list[object]]:
        calls.append(query)
        if "count(r)" in query:
            return [[1200]]  # 1200 prunable runs → ceil(1200/500) = 3 bounded batches
        return []

    monkeypatch.setattr(repo_mod, "run_cypher", _fake)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    pruned = asyncio.run(repo.prune_runs("2020-01-01T00:00:00+00:00"))

    assert pruned == 1200
    deletes = [q for q in calls if "DETACH DELETE" in q]
    assert len(deletes) == 3  # ceil(1200 / 500) transactions, not one giant statement
    assert all("LIMIT 500" in q for q in deletes)  # every delete is bounded


def test_prune_runs_noop_when_nothing_old(monkeypatch: pytest.MonkeyPatch) -> None:
    import lineage.services.repository as repo_mod

    calls: list[str] = []

    async def _fake(_conn: object, _graph: str, query: str, params: dict[str, object] | None = None, *, columns: int = 1) -> list[list[object]]:
        calls.append(query)
        return [[0]] if "count(r)" in query else []

    monkeypatch.setattr(repo_mod, "run_cypher", _fake)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    assert asyncio.run(repo.prune_runs("2020-01-01T00:00:00+00:00")) == 0
    assert [q for q in calls if "DETACH DELETE" in q] == []  # no delete issued when nothing qualifies


def test_prune_batch_size_is_single_sourced(monkeypatch: pytest.MonkeyPatch) -> None:
    """One constant (``cypher.PRUNE_BATCH_SIZE``) drives BOTH the loop count and the per-batch delete size.

    Regression guard (F-LIN-06): the delete LIMIT was baked as a separate 500 literal in the Cypher
    string, so lowering the batch size shrank the loop count while every delete still cut 500 —
    under-pruning silently. With the size single-sourced, a batch of 2 over 5 old runs is ceil(5/2)=3
    transactions, each delete bounded at LIMIT 2."""
    import lineage.services.cypher as cypher_mod
    import lineage.services.repository as repo_mod

    monkeypatch.setattr(cypher_mod, "PRUNE_BATCH_SIZE", 2)
    calls: list[str] = []

    async def _fake(_conn: object, _graph: str, query: str, params: dict[str, object] | None = None, *, columns: int = 1) -> list[list[object]]:
        calls.append(query)
        return [[5]] if "count(r)" in query else []

    monkeypatch.setattr(repo_mod, "run_cypher", _fake)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    asyncio.run(repo.prune_runs("2020-01-01T00:00:00+00:00"))

    deletes = [q for q in calls if "DETACH DELETE" in q]
    assert len(deletes) == 3  # ceil(5 / 2), driven by the monkeypatched constant
    assert all("LIMIT 2" in q for q in deletes)  # and the delete size follows the SAME constant
    assert not any("LIMIT 500" in q for q in deletes)  # never the stale baked literal


# --------------------------------------------------------------------------- #
# §7 residual: the RUNNING progress facet — model parse, the conditional
# _SET_RUN_PROGRESS write at ingest, and the /runs fold that surfaces it.
# --------------------------------------------------------------------------- #


def _running_event(facets: dict[str, Any]) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "RUNNING",
            "eventTime": "2026-07-06T00:00:00Z",
            "run": {"runId": "r-prog", "facets": facets},
            "job": {"namespace": "lance-medallion", "name": "embed"},
            "inputs": [],
            "outputs": [],
        }
    )


def test_progress_facet_parses_only_when_both_fields_present() -> None:
    """``RunEvent.progress`` is our custom ``progress`` run facet (OpenLineage has no standard one):
    ``(done, total)`` when both ride, ``None`` for absent/partial/malformed — never a raise: a raise at
    ingest would poison-message the Dapr RETRY loop (the same malformed event redelivered forever)."""
    assert _running_event({"progress": {"done": 3, "total": 10}}).progress == (3, 10)
    assert _running_event({}).progress is None
    assert _running_event({"progress": {"done": 3}}).progress is None  # partial → None, not (3, ???)
    assert _running_event({"progress": "3/10"}).progress is None  # non-dict facet ignored
    # non-int-coercible fields → None, not a ValueError (the §7a poison-message case)
    assert _running_event({"progress": {"done": "3/10", "total": 10}}).progress is None
    assert _running_event({"progress": {"done": [3], "total": 10}}).progress is None


def test_ingest_sets_run_progress_only_when_the_facet_rides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress is SET in its own conditional statement — stamped with the facet's numbers when it
    rides, and NEVER issued without it (an unconditional SET would clobber the trail back to null)."""
    import lineage.services.repository as repo_mod

    calls: list[tuple[str, dict[str, object]]] = []

    async def _capture(_conn: object, _graph: str, query: str, params: dict[str, object], *, columns: int = 1) -> list[list[object]]:
        del columns  # signature parity with the real helper (#49 reads governance during ingest)
        calls.append((query, params))
        return []  # a read (the tag-union governance lookup) sees "no node yet" → union starts empty

    monkeypatch.setattr(repo_mod, "run_cypher", _capture)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")

    asyncio.run(repo.ingest_event(_running_event({"progress": {"done": 3, "total": 10}})))
    progress_sets = [p for q, p in calls if "SET r.progress_done" in q]
    assert progress_sets == [{"rid": "r-prog", "pd": 3, "pt": 10}]

    calls.clear()
    asyncio.run(repo.ingest_event(_running_event({})))  # a later RUNNING without the facet
    assert [p for q, p in calls if "SET r.progress_done" in q] == []  # trail preserved, not nulled


def test_list_runs_folds_progress_onto_the_status_board(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 14-column `cypher.LIST_RUNS` row folds into RunStatus with progress as ints (positions 4/5).

    The column count is asserted, not incidental: `fetch(..., columns=N)` is POSITIONAL, so an
    off-by-one silently shifts every field on the run board rather than failing — a wrong author
    against the wrong job, with nothing red. It went 13 -> 14 when `promotion_status` joined, so the
    run board could tell a HELD promotion from a hard failure.
    """
    import lineage.services.repository as repo_mod

    async def _fake_fetch(_pool: object, _graph: str, query: str, params: dict[str, object] | None = None, *, columns: int) -> list[list[object]]:
        # 14 columns since promotion_status joined the projection (the held-vs-failed fix).
        assert "r.progress_done" in query and columns == 14
        return [
            [
                "r-prog",
                "lance-medallion/embed",
                "data_eng",
                "RUNNING",
                3,
                10,
                "",
                "t0",
                "t1",
                2,
                "silver$features",
                "",
                "ingest-run-77",
                "",
            ]
        ]

    monkeypatch.setattr(repo_mod, "fetch", _fake_fetch)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    runs = asyncio.run(repo.list_runs()).runs
    assert len(runs) == 1
    assert (runs[0].progress_done, runs[0].progress_total) == (3, 10)
    assert runs[0].state == "RUNNING"
    assert runs[0].operation is None  # "" maps back to None (no lance-facet operation)
    assert runs[0].promotion_status is None  # likewise: an ordinary run refused no promotion


# --------------------------------------------------------------------------- #
# Batch 5 (2026-07-11): terminal lifecycle stamp + column-inventory GC
# --------------------------------------------------------------------------- #


def test_dropped_at_derives_from_the_latest_successful_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # ASSERTS: dropped_at() is READ-TIME DERIVATION over run history (never a stored flag — a
    # stored flag was last-delivery-wins under redelivery): latest successful op drop_table → the
    # drop's event time; latest successful op create_table (the recreate) → None; no runs → None.
    # Also pins the load-bearing filter: FAILed runs keep WROTE edges, so the query MUST restrict
    # to event_type COMPLETE or a failed drop would count.
    import lineage.services.repository as repo_mod

    seen_queries: list[str] = []
    canned: list[list[object]] = []

    async def _fake_fetch(_pool: object, _graph: str, query: str, params: dict, *, columns: int = 1):
        seen_queries.append(query)
        assert params == {"name": "table:doomed"} and columns == 2
        return canned

    monkeypatch.setattr(repo_mod, "fetch", _fake_fetch)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")

    canned[:] = [["drop_table", "2026-07-11T09:00:00Z"]]
    assert asyncio.run(repo.dropped_at("table:doomed")) == "2026-07-11T09:00:00Z"

    canned[:] = [["create_table", "2026-07-11T10:00:00Z"]]  # the recreate outranks the drop
    assert asyncio.run(repo.dropped_at("table:doomed")) is None

    canned[:] = []
    assert asyncio.run(repo.dropped_at("table:doomed")) is None

    assert all("r.event_type = 'COMPLETE'" in q for q in seen_queries)  # failed drops assert nothing


def _capture_with_graph_version(monkeypatch: pytest.MonkeyPatch, event: dict[str, Any], graph_version: str | None) -> list[tuple[str, dict]]:
    """Ingest one event; the fake answers the latest-WROTE-version probe with ``graph_version``."""
    import lineage.services.repository as repo_mod

    calls: list[tuple[str, dict]] = []

    async def _capture(_conn: object, _graph: str, query: str, params: dict) -> list[list[object]]:
        calls.append((query, params))
        if "ORDER BY r.event_time DESC" in query and "w.version" in query:
            return [[graph_version]] if graph_version is not None else []
        return []

    monkeypatch.setattr(repo_mod, "run_cypher", _capture)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    asyncio.run(repo.ingest_event(RunEvent.model_validate(event)))
    return calls


def _overwrite_event(version: str) -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-07-11T09:00:00Z",
        "run": {"runId": "o1", "facets": {"lance": {"operation": "create_table"}}},
        "job": {"namespace": "lance-catalog", "name": "create_table"},
        "outputs": [
            {
                "namespace": "silver",
                "name": "silver$features",
                "facets": {
                    "version": {"datasetVersion": version},
                    "schema": {"fields": [{"name": "x", "type": "int64"}, {"name": "y", "type": "double"}]},
                    "columnLineage": {"fields": {"z": {"inputFields": [{"namespace": "bronze", "name": "bronze$events", "field": "id"}]}}},
                },
            }
        ],
    }


def test_ingest_schema_facet_prunes_stale_column_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    # ASSERTS: an output at version 5 (>= the graph's latest, 4) carrying schema {x,y} + a
    # columnLineage out_field z issues the HAS_COLUMN unlink with fields == [x,y,z] (the COMPLETE
    # current set) — an overwrite's replaced columns ({a,b}->{x,y}) stop being listed as CURRENT.
    calls = _capture_with_graph_version(monkeypatch, _overwrite_event("5"), graph_version="4")
    unlinks = [p for q, p in calls if "DELETE r" in q and "HAS_COLUMN" in q]
    assert unlinks == [{"ds": "silver$features", "fields": ["x", "y", "z"]}]


def test_ingest_stale_redelivery_neither_prunes_nor_reseeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # ASSERTS: the recency gate covers the WHOLE inventory touch — a redelivered OLD event
    # (version 2 < the graph's latest, 9) must neither UNLINK the live columns NOR re-ADD its own
    # stale ones (live-AGE CI catch 2026-07-11: gating only the prune let the stale event's
    # grow-only MERGEs resurrect ['a','b'] beside the live ['x','y']). Its column-lineage EDGES
    # are still ingested (history), but no schema seeding happens.
    calls = _capture_with_graph_version(monkeypatch, _overwrite_event("2"), graph_version="9")
    assert not any("DELETE r" in q for q, _ in calls)
    seeded = [p["fld"] for q, p in calls if "c.type" in q]  # _MERGE_COLUMN_TYPED = schema seeding
    assert seeded == []  # the stale schema fields x,y were NOT re-added to the inventory


def test_ingest_versionless_schema_event_seeds_but_never_prunes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ASSERTS: a version-LESS schema event (external producers — ordering unknowable) keeps the
    # legacy grow-only behavior: fields ARE seeded (no inventory regression) but NOTHING is pruned.
    event = _overwrite_event("2")
    del event["outputs"][0]["facets"]["version"]
    calls = _capture_with_graph_version(monkeypatch, event, graph_version="9")
    seeded = {p["fld"] for q, p in calls if "c.type" in q}
    assert {"x", "y"} <= seeded  # seeded despite no version
    assert not any("DELETE r" in q for q, _ in calls)  # but never pruned


def test_ingest_without_schema_facet_never_prunes(monkeypatch: pytest.MonkeyPatch) -> None:
    # ASSERTS: no schema facet = PARTIAL knowledge of the column set — pruning is skipped entirely
    # (a facet-less mover event must never wipe the inventory a create built).
    event = {
        "eventType": "COMPLETE",
        "eventTime": "2026-07-11T09:00:00Z",
        "run": {"runId": "m1", "facets": {"lance": {"operation": "ingest_events"}}},
        "job": {"namespace": "lance-medallion", "name": "ingest_events"},
        "outputs": [{"namespace": "bronze", "name": "bronze$events"}],
    }
    calls = _capture_with_graph_version(monkeypatch, event, graph_version="1")
    assert not any("DELETE r" in q for q, _ in calls)


def test_input_version_reads_the_pinned_feature_version() -> None:
    """A PINNED input exposes the version it consumed — the read twin of ``output_version``.

    The Ray TRAIN job pins every feature dataset (#115 D1: training on floating LATEST is not
    reproducible) and emits the pin as a spec ``version`` facet on the INPUT. Before 2026-07-13 the
    ingest dropped it (280 READ edges in the live graph, zero versions), so the graph could not answer
    the question the pin exists to answer: which exact feature versions produced this model.
    """
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-13T09:00:00+00:00",
            "producer": "https://github.com/Borg93/lance-ns",
            "run": {"runId": "11111111-1111-5111-8111-111111111111", "facets": {}},
            "job": {"namespace": "ray-jobs", "name": "train.churn"},
            "inputs": [
                {
                    "namespace": "silver",
                    "name": "silver$features",
                    "facets": {"version": {"datasetVersion": "28"}},
                },
                {"namespace": "gold", "name": "gold$catalog", "facets": {}},  # unpinned read
            ],
            "outputs": [
                {
                    "namespace": "models",
                    "name": "models$churn",
                    "facets": {"version": {"datasetVersion": "3"}},
                }
            ],
        }
    )
    assert event.input_version("silver$features") == "28"
    assert event.input_version("gold$catalog") is None  # floating read → no pin to record
    assert event.input_version("not-an-input") is None
    assert event.output_version("models$churn") == "3"  # the write twin still works


class _DDLConn:
    """Records executed SQL + transaction boundaries (the first-boot DDL runs no Cypher).

    ``fail_on`` raises ``QueryCanceled`` on the first matching statement — a served statement
    timeout, to prove the fail-closed boot path.
    """

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self._fail_on = fail_on

    async def execute(self, statement: object, params: object = None) -> None:
        del params
        text = statement.as_string() if isinstance(statement, sql.Composable) else str(statement)
        self.calls.append(text)
        if self._fail_on and self._fail_on in text:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    def transaction(self) -> Any:
        conn = self

        class _Tx:
            async def __aenter__(self) -> _Tx:
                conn.calls.append("BEGIN")
                return self

            async def __aexit__(self, *_a: object) -> bool:
                conn.calls.append("END")
                return False

        return _Tx()


class _DDLPool:
    """Minimal async pool whose ``connection()`` yields one recording ``_DDLConn`` (no DB)."""

    def __init__(self, conn: _DDLConn) -> None:
        self._conn = conn

    def connection(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> _DDLConn:
                return conn

            async def __aexit__(self, *_a: object) -> bool:
                return False

        return _Ctx()


def test_ensure_events_table_runs_ddl_under_set_local_timeout() -> None:
    """The first-boot DDL opens its transaction with SET LOCAL statement_timeout at the configured
    value (P6): the bound rides the transaction (so nothing leaks to the pooled session) and covers
    every statement — a wedged/locked Postgres cancels the boot instead of hanging it forever."""
    import lineage.services.repository as repo_mod

    conn = _DDLConn()
    repo = repo_mod.LineageRepository(cast(Any, _DDLPool(conn)), "g", statement_timeout_seconds=5.0)
    asyncio.run(repo.ensure_events_table())

    # SET LOCAL is the transaction's first statement, at the configured seconds → milliseconds.
    assert conn.calls[:2] == ["BEGIN", "SET LOCAL statement_timeout = 5000"]
    # Every DDL statement runs inside the same transaction, after the timeout is in force.
    ddl = conn.calls[2:-1]
    assert conn.calls[-1] == "END"
    assert any("CREATE TABLE IF NOT EXISTS public.lineage_events" in s for s in ddl)
    assert sum("DELETE FROM public.lineage_events" in s for s in ddl) == 2  # both dedup passes
    assert sum("CREATE UNIQUE INDEX" in s for s in ddl) == 2  # natural + terminal keys


def test_ensure_events_table_timeout_raises_fail_closed() -> None:
    """A statement timeout (QueryCanceled) propagates out of the lifespan — fail-closed boot /
    crash-loop, never a half-built feed; only the DuplicateTable create race is swallowed."""
    import lineage.services.repository as repo_mod

    conn = _DDLConn(fail_on="DELETE FROM public.lineage_events")
    repo = repo_mod.LineageRepository(cast(Any, _DDLPool(conn)), "g")
    with pytest.raises(psycopg.errors.QueryCanceled):
        asyncio.run(repo.ensure_events_table())


def test_a_held_promotion_is_distinguishable_from_a_failed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the column: eventType is FAIL for both, and only this tells them apart.

    A promotion the quality gate HELD is a question for a validator; a BLOCKED one is corrupt and no
    approval can waive it. Both emit `eventType=FAIL`, correctly — the promotion genuinely did not
    advance, and every existing FAIL consumer must keep meaning that. Before this column crossed the
    wire the run board had no way to distinguish them and rendered both as failures, so a hold looked
    like an outage and a corrupt batch looked approvable.
    """
    import lineage.services.repository as repo_mod

    async def _fake_fetch(_pool: object, _graph: str, query: str, params: dict[str, object] | None = None, *, columns: int) -> list[list[object]]:
        assert "r.promotion_status" in query, "the projection does not return the verdict at all"
        return [
            ["r-held", "lance-medallion/promote", "data_eng", "FAIL", None, None, "quality gate HELD …", "t0", "t1", 1, "silver$features", "", "", "HELD"],
            ["r-dead", "lance-medallion/promote", "data_eng", "FAIL", None, None, "boom", "t0", "t1", 1, "silver$features", "", "", ""],
        ]

    monkeypatch.setattr(repo_mod, "fetch", _fake_fetch)
    repo = repo_mod.LineageRepository(cast(Any, _FakePool()), "g")
    runs = asyncio.run(repo.list_runs()).runs

    held, dead = runs[0], runs[1]
    assert held.state == "FAIL" and dead.state == "FAIL", "both are terminal failures on the wire"
    assert held.promotion_status == "HELD"
    assert dead.promotion_status is None, '"" must fold back to None, not to a falsy verdict string'


def test_the_promotion_verdict_is_sticky_across_a_runs_events() -> None:
    """A later event carrying no verdict must not erase the one an earlier event declared.

    Same rule as `operation` and `source_run_id`, and for the same reason: a reconcile or backfill
    event for the same graph run carries no lance facet, and clobbering the verdict to null would
    turn a recorded hold back into an ordinary failure on the next tick.
    """
    import lineage.services.cypher as cypher_mod

    assert "r.promotion_status=(CASE WHEN $ps = '' THEN r.promotion_status ELSE $ps END)" in cypher_mod.MERGE_RUN


def test_the_run_event_reads_its_verdict_off_the_lance_facet() -> None:
    """Producer side: the verdict rides `lance.promotion_status`, beside operation and originator."""
    from lineage.models import RunEvent

    event = RunEvent.model_validate(
        {
            "eventType": "FAIL",
            "eventTime": "2026-08-26T00:00:00Z",
            "run": {"runId": "11111111-1111-5111-8111-111111111111", "facets": {"lance": {"operation": "promote", "promotion_status": "HELD"}}},
            "job": {"namespace": "lance-medallion", "name": "promote"},
        }
    )

    assert event.promotion_status == "HELD"

    without = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-08-26T00:00:00Z",
            "run": {"runId": "11111111-1111-5111-8111-111111111111", "facets": {}},
            "job": {"namespace": "lance-medallion", "name": "promote"},
        }
    )
    assert without.promotion_status is None


def test_pool_closed_when_bootstrap_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bootstrap step raising during lifespan startup must still close the AGE pool.

    Regression guard (F-LIN-09): `await pool.open()` and the bootstrap awaits used to sit BEFORE the
    `try:` whose `finally:` owns `await pool.close()`. A failure in any bootstrap step (here
    ensure_events_table) raised past that guard with the connection pool open and never closed —
    a leaked pool per crash-looping boot.
    """
    import types

    import lineage.main as main_mod

    class _LifecyclePool:
        def __init__(self) -> None:
            self.opened = False
            self.closed = False

        async def open(self) -> None:
            self.opened = True

        async def close(self) -> None:
            self.closed = True

    pool = _LifecyclePool()

    class _BoomRepo:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def ensure_events_table(self) -> None:
            raise RuntimeError("bootstrap boom")

    settings = types.SimpleNamespace(
        audit_enabled=False,
        dapr_enabled=False,
        reconcile_binding_name=None,
        database_url="postgresql://x",
        age_statement_timeout_seconds=30.0,
        graph="g",
        events_retention=None,
        outbox_uri="",
    )

    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "configure_audit", lambda **_k: None)
    monkeypatch.setattr(main_mod, "instrument_lance_if_available", lambda: None)
    monkeypatch.setattr(main_mod, "assert_app_token_configured", lambda **_k: None)
    monkeypatch.setattr(main_mod, "apply_lineage_secrets", lambda _s: None)
    monkeypatch.setattr(main_mod, "make_pool", lambda *_a, **_k: pool)
    monkeypatch.setattr(main_mod, "LineageRepository", _BoomRepo)

    app = types.SimpleNamespace(state=types.SimpleNamespace())

    async def _drive() -> None:
        async with main_mod.lifespan(cast(Any, app)):
            pass

    with pytest.raises(RuntimeError, match="bootstrap boom"):
        asyncio.run(_drive())

    assert pool.opened is True
    assert pool.closed is True  # the finally must run even when a bootstrap await raises


def test_demo_read_failure_is_logged(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A failed S3/pylance read in the demo peek must be logged, not swallowed silently.

    Regression guard (F-LIN-11): demo.py declared a module logger and never used it — the three read
    arms returned a fallback on a bare `except Exception` with no record, so a persistent storage
    outage looked like an empty demo rather than a fault. The read now logs a warning AND narrows the
    catch, so a programming error (AttributeError/TypeError) surfaces instead of being masked.
    """
    import logging

    from lineage.api.v1.endpoints import demo

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("s3 unreachable")

    monkeypatch.setattr(demo.lance, "dataset", _boom)
    with caplog.at_level(logging.WARNING, logger="lineage.api.v1.endpoints.demo"):
        result = demo._read_dataset(demo.PeekCache(), "bronze$events", "s3://bucket/bronze/events", {}, 5)

    assert result.exists is False  # unchanged: a read failure still yields the "not present" payload
    assert any(rec.levelno == logging.WARNING for rec in caplog.records), "the swallowed read was not logged"


def test_demo_read_reraises_programming_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrowed catch lets a genuine bug through instead of reporting the dataset absent.

    Regression guard (F-LIN-11): a bare `except Exception` masked AttributeError/TypeError as
    exists=False. Narrowing to the concrete read/IO errors surfaces the bug.
    """
    from lineage.api.v1.endpoints import demo

    def _bug(*_a: object, **_k: object) -> object:
        raise AttributeError("typo in the peek code")

    monkeypatch.setattr(demo.lance, "dataset", _bug)
    with pytest.raises(AttributeError, match="typo in the peek code"):
        demo._read_dataset(demo.PeekCache(), "bronze$events", "s3://bucket/bronze/events", {}, 5)
