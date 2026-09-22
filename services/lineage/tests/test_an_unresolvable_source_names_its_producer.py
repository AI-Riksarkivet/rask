"""An event recording a location nothing can resolve says WHO recorded it.

[[LH-187]]. The sweep has refused a relative `dataSource` URI since it was written — "a relative path
cannot say whether the data is there" — but only at read time and only as a count, so the estate
stored exactly what it would later refuse, every tick forever, and the refusal named the DATASET
rather than the producer. Measured on the live estate 2026-09-22: `unreadable=23` on a `checked=483`
tick, with nothing linking any of them to a writer.

ATTRIBUTION HAS TO HAPPEN AT INGEST because that is the only moment the producer exists. By the time
the sweep reads the graph the event is gone.

AND IT HAS TO HAPPEN ON THE SHARED SEAM, not at a door. `repository.ingest_event` is reached by FOUR
paths (HTTP, the JetStream consumer, the DLQ replay door, the reconcile relay) and the HTTP door's
own comment records what putting a report on one of them costs: "counting it only on the subscriber
made the same loss alertable on one door and invisible on the other".
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from lineage.core.source_uri import names_a_storage_location, unresolvable_sources
from lineage.models import RunEvent


def _event(uri: str | None, *, facet_producer: str | None = "https://producer.invalid/emitter") -> RunEvent:
    facets = {}
    if uri is not None:
        facets["dataSource"] = {"name": "ds", "uri": uri, "_producer": facet_producer, "_schemaURL": "s"}
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-22T08:00:00Z",
            "producer": "https://envelope.invalid/relay",
            "job": {"namespace": "rask", "name": "j"},
            "run": {"runId": "00000000-0000-0000-0000-000000000001"},
            "outputs": [{"namespace": "rask", "name": "t", "facets": facets}],
        }
    )


@pytest.mark.parametrize("uri", ["s3://lakehouse/silver/features", "/srv/lake/t.lance", "file:///srv/lake/t.lance"])
def test_a_resolvable_uri_is_not_reported(uri: str) -> None:
    """The normal case, and it must stay silent — a report on every healthy write is a report nobody reads."""
    assert names_a_storage_location(uri)
    assert unresolvable_sources(_event(uri)) == []


@pytest.mark.parametrize("uri", ["4750a5b9_acme-bronze$events", "transcripts_v2.lance/chunks.lance", "t1.lance"])
def test_a_BARE_NAME_is_reported_with_the_uri_that_caused_it(uri: str) -> None:
    """All three shapes are live offenders read off the estate 2026-09-22, including the nested one the
    `dir` backend's flat `<uuid>_<table>` layout never produces."""
    assert not names_a_storage_location(uri)

    found = unresolvable_sources(_event(uri))

    assert [f.uri for f in found] == [uri]
    assert found[0].dataset == "rask$t", "the report does not name the dataset it is about"


def test_the_FACET_producer_wins_over_the_envelope() -> None:
    """A relayed event carries the relay as `producer` and the original emitter on the facet. Naming the
    relay would send whoever reads the report to the wrong service — and the reconcile relay re-ingests
    every drained outbox event, so the two differ on a path that runs every tick."""
    found = unresolvable_sources(_event("bare", facet_producer="https://producer.invalid/emitter"))

    assert found[0].producer == "https://producer.invalid/emitter"


def test_the_ENVELOPE_producer_is_the_fallback_when_the_facet_names_none() -> None:
    """`_producer` is optional on a facet. Reporting `<unnamed>` while the envelope names someone would
    throw away the one attribution available."""
    found = unresolvable_sources(_event("bare", facet_producer=None))

    assert found[0].producer == "https://envelope.invalid/relay"


def test_a_dataset_carrying_NO_dataSource_facet_is_not_reported() -> None:
    """The facet is OPTIONAL in OpenLineage. An absent location is not an unresolvable one, and the
    sweep already skips those for the same reason — conflating them would report most of the estate."""
    assert unresolvable_sources(_event(None)) == []


def test_the_SHARED_SEAM_reports_it_rather_than_one_door() -> None:
    """Driven off the source because the alternative is a live AGE pool.

    `ingest_event` is the one call all four ingest paths make. A report added to the HTTP door instead
    would leave the JetStream consumer, the DLQ replay and the reconcile relay silent — which is the
    exact asymmetry that door's own comment was written about.
    """
    repository = pathlib.Path("services/lineage/src/lineage/services/repository.py")
    tree = ast.parse(repository.read_text(encoding="utf-8"))
    ingest = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "ingest_event")
    called = {inner.func.id for inner in ast.walk(ingest) if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)}

    assert "unresolvable_sources" in called, (
        "`repository.ingest_event` does not report unresolvable dataset locations, so the four ingest "
        "paths record a URI every later sweep refuses with nothing naming the producer"
    )
