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

from typing import Any

import pytest
from openlineage.client.serde import Serde

from ingest.lineage import LineageRecorder, _output_datasets


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


def test_a_FAILED_run_still_stamps_what_it_MANAGED_to_write() -> None:
    """The case an operator actually needs, and the reason facets are not COMPLETE-only.

    A run that commits fragments and then dies has moved the dataset forward. The version it left
    behind is precisely what reconstructing the damage requires — and if the graph withholds it
    because the run's status was FAILED, the only record of a real, committed, half-finished write is
    a log line.
    """
    out = _output(version=3, rows=88)

    assert out["facets"]["version"]["datasetVersion"] == "3"
    assert out["outputFacets"]["outputStatistics"]["rowCount"] == 88


def test_the_output_pair_is_the_TIER_QUALIFIED_one_the_cascade_head_matches() -> None:
    """#52 — and the bug this replaces is the reason the whole plane could look healthy and be inert.

    The head fires on an event whose outputs contain `project_namespace(project, bronze_namespace)`
    and the matching name (`ingest_trigger.py:52-57`). This used to emit the PROJECT as the namespace
    — `bind86` / `bind86$pages` — which never intersects `bind86-bronze` / `bind86-bronze$pages`. No
    ingest write could fire the cascade, and nothing failed while that was true: the data landed,
    lineage recorded it, A8 passed, silver was simply never woken.

    A project selects the storage ROOT; the namespace is the TIER; the table is the dataset.
    """
    out = _output()

    assert out["namespace"] == "bind86-bronze"
    assert out["name"] == "bind86-bronze$pages"


def test_NO_project_degrades_to_the_single_tenant_pair() -> None:
    """`project_namespace("", "bronze")` is `bronze` — byte-identical to the pre-#84 single-tenant
    estate, which is what the head falls back to when an event carries no `lance.project` facet. The
    two sides have to degrade the same way or the fallback misses in exactly the case it exists for."""
    out = _output(project="")

    assert out["namespace"] == "bronze"
    assert out["name"] == "bronze$pages"


def test_an_UNSAFE_project_degrades_rather_than_becoming_a_path_segment() -> None:
    """The head refuses a project outside the path-safe shape (`_cascade_project` -> `is_safe_project`)
    because it would otherwise become an S3 prefix or a lineage-name qualifier. The writer applies the
    SAME guard: a garbage project must not produce a write that fires the head for a tenant, and must
    not produce one the head ignores for a reason invisible from here."""
    out = _output(project="../etc")

    assert out["namespace"] == "bronze", "an unsafe project must not reach the namespace"
    assert ".." not in out["name"]


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


def test_the_terminal_emit_is_a_checkpointed_ACTIVITY_not_an_inline_call() -> None:
    """A6's last clause — "the outbox object is dropped after publish" — reached structurally.

    There is no outbox in the shipped design, and this asserts the mechanism that replaces it rather
    than papering over its absence. An outbox exists to stop a crash between the COMMIT and the EMIT
    from losing the record permanently. Here the emit is a Dapr Workflow ACTIVITY: the runtime
    checkpoints activity completion in its state store and re-executes any activity that did not
    RETURN, so the workflow cannot advance past the terminal emit without that activity having RUN.
    Re-delivery is safe because `lineage_run_id` is a deterministic uuid5 — a replayed emit rewrites
    the same run rather than forking a second one.

    **WHAT THE CHECKPOINT ATTESTS, and what it does not**. It attests that the
    activity ran to completion. It does NOT attest that the event was DELIVERED: `lineage._emit`
    swallows a failed emission on purpose (I8 — "a run whose data landed must not be reported as
    failed because the graph was unreachable"), so the activity returns successfully whether or not
    the lineage service ever saw the event. This docstring used to read "the orchestrator IS the
    outbox", which claims the delivery guarantee an outbox gives and this does not have. The
    difference is exactly the crash window an outbox closes and this design accepts, by ruling
    (§2.22 kept the swallow); saying otherwise here made the accepted risk look eliminated.

    Move this call inline into the orchestrator body and that guarantee silently evaporates: it would
    still work, right up to the first crash between commit and emit.
    """
    import inspect

    from dapr.ext.workflow import WorkflowActivityContext

    from ingest.workflow import emit_terminal

    first = next(iter(inspect.signature(emit_terminal).parameters.values()))
    assert first.annotation in (WorkflowActivityContext, "WorkflowActivityContext"), (
        "emit_terminal stopped being an activity — a crash between commit and emit now loses the provenance record"
    )


@pytest.mark.parametrize("project", ["", "bind86"])
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


def test_the_ingest_run_names_the_HUMAN_who_asked_for_it() -> None:
    """THE INGEST LANE'S TARGETING DEFECT.

    An ingest run reaches lineage over HTTP, where `enforce_author` overwrites the author facet with
    the CALLER's verified sub — and the caller is this service, presenting
    `RASK_LINEAGE_SERVICE_IDENTITY: service-ingest` (`chart/values.yaml:161`). So every ingest run in
    the estate was announced to an inbox named `service-ingest`, and the person who asked for the
    harvest was told nothing about their own run finishing or failing.

    That is the WORST shape in the register precisely because it looks covered: a row IS delivered on
    every run, so nothing anywhere reads as broken — the plane simply tells a service instead of a
    person. The medallion had the same defect with a role literal; `lance.originator` is the field
    that fixed it, and notifications reads it from any producer that stamps it (`originator_subject`).

    `RunSpec`'s own docstring already claimed this: "the request, plus the identity minted at accept".
    The identity was minted and then dropped.
    """
    from ingest.lineage import _tenant_facet

    facet = _tenant_facet("bind86", "run-123", originator="alice")["lance"]
    assert facet["originator"] == "alice"


def test_an_ingest_run_without_an_originator_is_unchanged() -> None:
    """A service-token call has no human behind it, and a placeholder there would re-create the very
    defect this closes — an inbox addressed to something that is not a person. Absent, not invented."""
    from ingest.lineage import _tenant_facet

    assert "originator" not in _tenant_facet("bind86", "run-123")["lance"]


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


def _refused() -> dict[str, Any]:
    """The verdict `_publish` really returns for a refused gate — taken from the function, not typed
    out here, so a change to its keys fails these tests instead of silently bypassing them."""
    from ingest.runtime import _publish

    class _Spec:
        project = "bind86"
        dataset = "pages"
        run_id = "run-1"

        @property
        def namespace(self) -> str:
            from ingest.naming import bronze_namespace_for

            return bronze_namespace_for(self.project)

    class _Catalog:
        def publish(self, _ns: str, _ds: str, _v: int) -> dict[str, Any]:
            return {"published": False, "from_version": 3, "to_version": 4, "reason": "quality gate failed: not_null"}

    return _publish(_Catalog(), _Spec(), 4)


def test_a_refused_publication_survives_the_RunOutcome_boundary() -> None:
    """The drop happens HERE, and it is silent: `RunOutcome` is a plain `BaseModel`, so pydantic's
    default `extra="ignore"` discards every publication key at `workflow.py`'s `model_validate`.

    Asserted on the object the emit path actually builds rather than on the dict `finalize_run`
    returns — the dict has always been right, which is exactly why nobody noticed."""
    from ingest.workflow import RunOutcome

    outcome = RunOutcome.model_validate({"committed_version": 4, "rows": 12, "errors": {}, "status": "COMPLETE", **_refused()})

    assert outcome.published is False, "the refusal was dropped crossing the activity boundary"
    assert outcome.publish_reason and "not_null" in outcome.publish_reason


def test_the_lance_facet_carries_a_refused_publication() -> None:
    """The graph's copy of the verdict. It rides the same `lance` custom facet as `originator`
    because that facet is `extra="allow"` end to end (`RunFacets.to_openlineage` merges
    `model_extra`), so a new key reaches the wire with no lineage-kit change."""
    from ingest.lineage import _tenant_facet

    facet = _tenant_facet("bind86", "run-1", published=False, publish_reason="quality gate failed: not_null")["lance"]

    assert facet["published"] is False
    assert "not_null" in facet["publish_reason"]


def test_a_published_run_says_so_rather_than_staying_silent() -> None:
    """`True` is stamped too, not just the refusal. A reader must be able to tell "published" from
    "this producer says nothing about publication" — if only failures were stamped, every event
    predating this change would read as a success."""
    from ingest.lineage import _tenant_facet

    assert _tenant_facet("bind86", "run-1", published=True)["lance"]["published"] is True


def test_nothing_to_commit_is_not_a_refusal() -> None:
    """`published=None` is a REAL third state (`runtime.py`: no version to gate, so `_publish` never
    ran). Collapsing it to `False` would report a gate refusal that never happened — the same class
    of lie this whole change exists to remove, pointing the other way."""
    from ingest.lineage import _tenant_facet

    assert "published" not in _tenant_facet("bind86", "run-1", published=None)["lance"]


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


def test_the_terminal_emit_forwards_the_verdict_it_was_given() -> None:
    """The link between the two halves. `terminal()` receives the verdict and must put it on the run
    it builds — a facet that is correct in isolation reaches nothing if the emit path drops it."""
    seen: dict[str, Any] = {}

    class _Run:
        def complete(self, **_: object) -> None:
            pass

        def fail(self, *_a: object, **_kw: object) -> None:
            pass

    def _capture(
        _run_id: str,
        project: str = "",
        originator: str = "",
        published: bool | None = None,
        publish_reason: str | None = None,
        publish_error: str | None = None,
    ) -> _Run:
        del project, originator  # positional placeholders — this test is about the verdict only
        seen.update({"published": published, "publish_reason": publish_reason, "publish_error": publish_error})
        return _Run()

    recorder = _Capture()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("ingest.lineage._run", _capture)
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

    assert seen.get("published") is False
    assert "not_null" in str(seen.get("publish_reason"))
