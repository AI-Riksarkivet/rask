"""A6 — a COMPLETE must NAME what it wrote, and say WHICH VERSION and HOW MUCH.

The hole this closes was invisible from every angle that mattered. `terminal()` has always been
handed the committed Lance version and the row count; it recorded both on its own in-memory
`LineageEvent`, and then emitted an OpenLineage output carrying NEITHER. So:

  * the run's own status endpoint reported `{"version": 7, "rows": 1204}` — correct, and local
  * the graph held `ingest.run -> wrote -> bind86$pages` with no version and no row count

Which reads as a working provenance record right up to the moment someone asks a real question of
it. "Where did version 7 come from" is answerable only with EVERY run that ever touched the table.
"Did the run that says it wrote 1204 rows actually write 1204 rows" is not answerable at all — the
number exists only in the reply the caller already has, which is the one place provenance is worth
nothing.

A8 could not catch it: A8 asks whether the run EXISTS in the graph, and it did. That is the same
shape as the 401 false-negative — a gate passing because it asks a narrower question than the one
its name implies.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from openlineage.client.serde import Serde

from ingest.catalog import ServiceCatalogSeam
from ingest.lineage import LineageRecorder, _output_datasets
from service_kit.lakehouse.vended_credentials import VendedCredential


def _output(project: str = "bind86", dataset: str = "pages", version: int | None = 7, rows: int = 1204) -> dict[str, Any]:
    """The output SERIALIZED, exactly as it goes on the wire.

    Asserting the pydantic model would let an aliasing bug through, and the aliases
    (`datasetVersion`, `rowCount`, `outputFacets`) are the whole contract — the graph and every
    OpenLineage consumer read those spellings, not the snake_case field names. Serializing rather
    than reaching for attributes also drops the client's own loose typing (`facets` is
    `dict[str, DatasetFacet] | None`, and the base facet declares none of these keys), so the
    assertions stay checkable instead of needing a suppression each.
    """
    outputs = _output_datasets(project, dataset, version, rows)
    assert len(outputs) == 1, "one run writes one table"
    return Serde.to_dict(outputs[0].to_openlineage_output())


class _Capture(LineageRecorder):
    """A recorder whose emits run INLINE, so a test can see what reached the transport.

    The real `_emit` swallows everything (I8: lineage must never fail a run that landed its data),
    which is correct in production and useless in a test — an emit that never happened and an emit
    that raised look identical from outside.
    """

    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[Any] = []

    def _emit(self, emit: Any) -> None:
        self.emitted.append(emit)
        emit()


def test_the_COMMITTED_VERSION_is_stamped_on_the_output() -> None:
    """The question the graph exists to answer: which version did this run produce.

    Without it the edge says only that a run touched a table, and reconstructing a dataset's history
    means correlating on timestamps across every writer — which is guessing with extra steps.
    """
    out = _output(version=7)

    assert out["facets"]["version"]["datasetVersion"] == "7"


def test_the_ROW_COUNT_is_stamped_as_outputStatistics() -> None:
    """The number that must not live only in the caller's reply.

    A run reporting 1204 rows into a void is unverifiable; the same number on the write edge can be
    reconciled against the dataset itself.
    """
    out = _output(rows=1204)

    assert out["outputFacets"]["outputStatistics"]["rowCount"] == 1204


def test_a_run_that_committed_NOTHING_writes_NO_version_facet() -> None:
    """`version=None` means no commit happened, and the honest record is SILENCE.

    Stamping `datasetVersion: "0"` or `"None"` would be worse than the original omission: the first
    is a version that exists and is wrong, the second is a string that parses as garbage downstream.
    A facet must never assert a fact the emitter does not have.
    """
    out = _output(version=None, rows=0)

    assert "version" not in out["facets"]
    assert out["outputFacets"]["outputStatistics"]["rowCount"] == 0, "zero rows is a MEASURED fact and stays"


def test_a_FAIL_carries_an_errorMessage_facet() -> None:
    """A6's first clause. A FAIL that names no reason is a record that the run ended, not why.

    The facet is built by `lineage-kit`'s `run.fail()`; what is asserted here is that ingest actually
    reaches that path with a message — an emit that passed an empty string would produce a
    spec-valid, useless event.
    """
    from lineage_kit.schemas import RunFacets

    assert "errorMessage" not in RunFacets().to_openlineage(), "sanity: the facet is absent unless a failure supplies it"

    messages: list[str] = []

    class _Run:
        def fail(self, error: str, **_: object) -> None:
            messages.append(error)

        def complete(self, **_: object) -> None:
            raise AssertionError("a FAILED run must not emit COMPLETE")

    recorder = _Capture()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ingest.lineage._run", lambda *_a, **_kw: _Run())
        recorder.terminal(run_id="run-42", status="FAILED", version=None, rows=0, errors={"unit-9": "corrupt tiff"}, project="bind86", dataset="pages")

    assert recorder.emitted, "a FAILED run emitted no terminal at all — the run leaves a record whichever way it ends"
    assert messages, "the terminal was emitted as something other than a FAIL"
    assert "corrupt tiff" in messages[0], "the failure's REASON must reach the facet, not just the fact of failure"


@pytest.mark.parametrize("project", ["bind86"])
def test_an_UNNAMEABLE_output_is_omitted_rather_than_half_written(project: str) -> None:
    """A run with no DATASET to name emits no output edge — not an edge with an empty name.

    `bind86-bronze$` would be a table id nothing in the estate resolves, and it would sit in the graph
    looking like a real one. Only the dataset is load-bearing here: an absent PROJECT is a legitimate
    single-tenant write (`bronze$<dataset>`), not an unnameable one, which is why it is no longer
    grounds for omitting the edge.
    """
    assert _output_datasets(project, "", 1, 1) == []


def test_the_run_facet_carries_the_INGEST_run_id() -> None:
    """The graph's run id is `run_id_for("ingest:<id>")` — a one-way UUID5 nothing can map back.

    Every row on the compute zone's run board linked with that derived id, and every link was dead
    (measured in a browser: "No such run" on each row — the ingest door answers only to its own id).
    So the producer states its own id as DATA, in the same `lance` facet the cascade head already
    reads; the repository folds it onto the Run node and `/runs` serves it as `source_run_id`.
    """
    from ingest.lineage import _tenant_facet

    facet = _tenant_facet("bind86", "run-123")["lance"]
    assert facet["run_id"] == "run-123"
    assert facet["project"] == "bind86"

    # Independent of the project guard ON PURPOSE: a single-tenant run still has an id worth linking,
    # and an unsafe project must drop the PROJECT, not the identity.
    unsafe = _tenant_facet("../etc", "run-123")["lance"]
    assert unsafe["run_id"] == "run-123"
    assert "project" not in unsafe

    # No id and no project -> no facet at all, exactly as before this field existed.
    assert _tenant_facet("../etc", None) == {}


def test_the_originator_survives_into_what_notifications_actually_reads() -> None:
    """The facet key is a contract with a consumer in ANOTHER deployable, so asserting the dict key
    alone would still pass if the reader looked somewhere else.

    Driven through notifications' own parser, exactly as the cross-service tests for the assignment
    lane are: what matters is that the plane which reads this wire JSON finds the person."""
    from ingest.lineage import _tenant_facet
    from notifications.api.lineage_events import LineageRunEvent, originator_subject

    event = {
        "eventType": "FAIL",
        "eventTime": "2026-08-16T12:00:00+00:00",
        "run": {"runId": "11111111-2222-4333-8444-555555555555", "facets": _tenant_facet("bind86", "run-9", originator="alice")},
        "outputs": [{"namespace": "bind86-bronze", "name": "bind86-bronze$pages"}],
    }
    assert originator_subject(LineageRunEvent.model_validate(event).run) == "alice"


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The publication VERDICT — a committed-but-refused run must not announce itself as a published one
# ─────────────────────────────────────────────────────────────────────────────────────────────────
#
# `finalize_run` already computes the verdict correctly and the PULL surface already renders it
# ("Committed, but not published — <reason>"). It dies on the way to the PUSH surface: `emit_terminal`
# re-validates the finalize output through `RunOutcome`, which declares none of the publication keys,
# and pydantic's default `extra="ignore"` drops them without a word. What reaches the graph is then
# byte-identical to a run that published — so the durable record, which `RunListResponse` itself names
# as the authoritative history, cannot tell the two apart.


def _publication_verdict() -> dict[str, Any]:
    """The verdict `_publish` really returns — taken from the function, not typed out here, so a change
    to its keys fails these tests instead of silently bypassing them.

    It was `_refused` while this plane still asked the catalog to publish its bronze. It no longer does
    — bronze is the cascade's root tier and is announced by its write event rather than promoted — so
    the verdict is now "the gate did not run". Renamed with the behaviour: a fixture called `_refused`
    that returns a non-refusal is the stale naming this estate keeps paying for.
    """
    from ingest.runtime import _publish

    class _Spec:
        project = "bind86"
        dataset = "pages"
        run_id = "run-1"

        @property
        def namespace(self) -> str:
            from ingest.naming import bronze_namespace_for

            return bronze_namespace_for(self.project)

    class _Catalog(ServiceCatalogSeam):
        # The members this test does not exercise RAISE rather than return a plausible value: a double
        # that answers every call lets an unintended one through unnoticed, which is what these tests
        # exist to detect.
        def publish(self, namespace: str, dataset: str, version: int, *, key_column: str = "id", required_columns: Sequence[str] = ()) -> dict[str, object]:
            return {"published": False, "from_version": 3, "to_version": 4, "reason": "quality gate failed: not_null"}

        def vend_storage_options(self, namespace: str, dataset: str, *, tier: str = "read") -> VendedCredential | None:
            raise AssertionError(f"this double vends nothing; {namespace}.{dataset} asked for a {tier} credential")

        def describe_version(self, namespace: str, dataset: str) -> int:
            raise AssertionError(f"this double describes no version; {namespace}.{dataset} was asked")

        def commit(self, namespace: str, dataset: str, fragments_json: Sequence[str], read_version: int, run_id: str) -> tuple[int, int]:
            raise AssertionError(f"this double commits nothing; run {run_id} tried against {namespace}.{dataset}")

        def ensure(self, namespace: str, dataset: str, external_base: str | None = None) -> str:
            raise AssertionError(f"this double ensures nothing; {namespace}.{dataset} was asked")

    return _publish(_Catalog(), _Spec(), 4)


def test_the_publication_verdict_survives_the_RunOutcome_boundary() -> None:
    """The drop happens HERE, and it is silent: `RunOutcome` is a plain `BaseModel`, so pydantic's
    default `extra="ignore"` discards every publication key at `workflow.py`'s `model_validate`.

    Asserted on the object the emit path actually builds rather than on the dict `finalize_run`
    returns — the dict has always been right, which is exactly why nobody noticed.

    THE VERDICT IS NOW "the gate did not run", not "the gate refused", and the boundary is what this
    test is about either way. Bronze is the cascade's root tier and is no longer offered for promotion,
    so `_publish` returns `published=None` with a reason; `None` is exactly the value pydantic's
    `extra="ignore"` would leave behind if the key were dropped, so the REASON is asserted beside it —
    without that, this test would pass against the bug it exists to catch.
    """
    from ingest.workflow import RunOutcome

    outcome = RunOutcome.model_validate({"committed_version": 4, "rows": 12, "errors": {}, "status": "COMPLETE", **_publication_verdict()})

    assert outcome.published is None, "a gate that never ran was reported as a refusal"
    assert outcome.publish_reason and "bronze" in outcome.publish_reason.lower(), "the verdict was dropped crossing the activity boundary"


def test_the_lance_facet_carries_a_refused_publication() -> None:
    """The graph's copy of the verdict. It rides the same `lance` custom facet as `originator`
    because that facet is `extra="allow"` end to end (`RunFacets.to_openlineage` merges
    `model_extra`), so a new key reaches the wire with no lineage-kit change."""
    from ingest.lineage import _tenant_facet

    facet = _tenant_facet("bind86", "run-1", published=False, publish_reason="quality gate failed: not_null")["lance"]

    assert facet["published"] is False
    assert "not_null" in facet["publish_reason"]


def test_a_refusal_does_not_become_a_FAIL() -> None:
    """THE CASCADE GUARD, and the reason this is a facet rather than a status change.

    The medallion's `/bronze-arrival` head fires only on `eventType == "COMPLETE"`. Flipping a
    refused run to FAIL would cancel the entire bronze->silver->gold cascade for a run whose data is
    committed and durable — and would lie in the other direction, because the RUN did its job. What
    was refused is the DATA.
    """
    calls: list[str] = []

    class _Run:
        def complete(self, **_: object) -> None:
            calls.append("complete")

        def fail(self, *_a: object, **_kw: object) -> None:
            calls.append("fail")

    recorder = _Capture()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ingest.lineage._run", lambda *_a, **_kw: _Run())
        recorder.terminal(
            run_id="run-1",
            status="COMPLETE",
            version=4,
            rows=12,
            errors={},
            project="bind86",
            dataset="pages",
            published=False,
            publish_reason="quality gate failed: not_null",
        )

    assert calls == ["complete"], "a refused publication is a COMPLETE run whose data was declined"
