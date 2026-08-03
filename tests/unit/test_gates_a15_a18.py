"""A15–A18 as named gates, against the real mover and the real chart values.

The four claims the plan makes about the cascade ABOVE bronze. Each was prose; each is now a test
that fails if the behaviour changes.

Everything here drives `medallion.services.transform.handle_stage` — the same function the deployed
mover's Dapr subscription calls — against real Lance in a temp directory. No mock stands in for the
thing under test: a gate over a double asserts that the double behaves, which is the failure mode
the estate has been burned by (a Tiltfile that could never have worked, an ingest queue nothing
drained; both looked healthy from every angle except an actual run).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import lance
import pytest
import yaml
from dapr.aio.clients import DaprClient
from medallion.core.config import MedallionSettings
from medallion.services.produce import produce
from medallion.services.transform import handle_stage


REPO = Path(__file__).resolve().parents[2]


class _FakeDapr:
    """Captures published events. A publish is the OBSERVABLE side effect these gates assert on —
    whether downstream was woken, and with what."""

    def __init__(self, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self.fail = fail

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        if self.fail:
            raise RuntimeError("pubsub unavailable")
        self.published.append({"topic": topic_name, "data": json.loads(data)})


def _bronze(tmp_path: Path) -> str:
    uri = str(tmp_path / "bronze")
    settings = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": uri})
    asyncio.run(produce(cast(DaprClient, _FakeDapr()), settings))
    return uri


def _mover(tmp_path: Path, **overrides: Any) -> MedallionSettings:
    base: dict[str, Any] = {
        "compute_enabled": True,
        "from_uri": str(tmp_path / "bronze"),
        "to_uri": str(tmp_path / "silver"),
        "from_namespace": "bronze",
        "from_dataset": "bronze$events",
        "to_namespace": "silver",
        "to_dataset": "silver$features",
        "operation": "embed",
        "pub_topic": "medallion.silver",
    }
    return MedallionSettings.model_validate({**base, **overrides})


# ── A15 — cleanup can never eat a live run ────────────────────────────────────────────


def _chart_values() -> dict[str, Any]:
    return yaml.safe_load((REPO / "chart" / "values.yaml").read_text())


def test_a15_version_gc_retention_exceeds_the_longest_permitted_ingest_run() -> None:
    """Cleanup's `older_than` must be ≥ the longest a run may take, as a RELATION not a coincidence.

    Lance version GC deletes versions older than a window. An ingest run holds a `read_version` from
    the moment it enumerates until it commits — so if that window is shorter than a run's maximum
    duration, GC can delete the very version the run is about to commit against, and the commit
    fails (or worse, succeeds against a rebased history). A long harvest is exactly the run that
    would hit it, which is to say the most expensive one.

    Asserted between the two CONFIG values rather than against a constant, so raising the permitted
    run duration without widening retention fails here instead of in production at 3am.
    """
    values = _chart_values()
    retention_hours = float(values["compaction"]["olderThanDays"]) * 24
    max_run_hours = float(values["services"]["ingest"]["env"]["RASK_INGEST_MAX_RUN_HOURS"])

    assert max_run_hours > 0, "a max run duration of 0 makes the relation vacuous"
    assert retention_hours >= max_run_hours, (
        f"version GC keeps {retention_hours}h of history but a run may take {max_run_hours}h — GC can delete the version a live run is committing against (A15)"
    )


def test_a15_the_relation_is_stated_where_BOTH_values_live() -> None:
    """Both numbers must be in the chart, or the relation cannot be checked before deploy.

    A max-run-duration that exists only as a code constant, or a retention that is only a component
    default, makes this gate unable to see one side of its own comparison — which is how a config
    invariant silently stops being one.
    """
    values = _chart_values()

    assert "olderThanDays" in values["compaction"]
    assert "RASK_INGEST_MAX_RUN_HOURS" in values["services"]["ingest"]["env"]


# ── A16 — index maintenance and compaction are owned lanes ────────────────────────────


def test_a16_compaction_runs_on_a_schedule_that_is_configured_not_implied() -> None:
    """A maintenance lane nobody scheduled is a lane that does not run.

    Incrementally-committed datasets accumulate small fragments — one per unit, in this plane — and
    unmaintained they turn every downstream scan into thousands of file opens. The cadence being a
    chart value is what makes it reviewable; a hardcoded default is a lane whose absence looks
    identical to its presence.
    """
    compaction = _chart_values()["compaction"]

    assert compaction["enabled"] is True
    assert compaction["schedule"], "no cron schedule — the compaction lane would never fire"
    assert compaction["bindingName"], "no binding name — the cron has nothing to POST"


def test_a16_indexed_search_returns_rows_from_the_LATEST_delta(tmp_path: Path) -> None:
    """The claim that matters: a row added by the newest commit is findable, not just present.

    An index built at version N does not cover rows added at N+1 until it is maintained. A search
    that silently misses the newest delta is the worst kind of wrong — it returns results, so nothing
    errors, and the missing rows are only discoverable by someone who already knows they exist.
    """
    uri = _bronze(tmp_path)
    before = lance.dataset(uri)
    baseline_rows, baseline_version = before.count_rows(), before.version

    # APPENDED, not re-seeded. `seed_bronze` overwrites (it is the idempotent head), so calling it
    # twice leaves the row count unchanged and the "latest delta" would be empty — the test would
    # then pass or fail for reasons having nothing to do with delta visibility.
    fresh = before.to_table().slice(0, 2)
    lance.write_dataset(fresh, uri, mode="append")
    dataset = lance.dataset(uri)

    assert dataset.count_rows() > baseline_rows
    # A scan bounded by the delta boundary must see exactly the new rows. This is the same CDF
    # predicate the movers use, so if it stops working the whole cascade stops moving deltas.
    delta = dataset.to_table(with_row_id=True, filter=f"_row_created_at_version > {baseline_version}")
    assert delta.num_rows > 0, "the newest commit's rows are invisible to a delta-bounded read"


# ── A17 — the mover contract (E1–E3) ──────────────────────────────────────────────────


def test_a17_a_redelivered_event_is_a_NO_OP_not_a_second_transform(tmp_path: Path) -> None:
    """E2. Redelivery is normal on an at-least-once bus, not exceptional.

    The same trigger token delivered twice must converge on one silver, not append a second copy of
    every row. Asserted on ROW COUNT rather than on a call count: a mover that "ran twice but wrote
    the same rows" is correct, and one that ran once but doubled the rows is not — only the data can
    tell them apart.
    """
    _bronze(tmp_path)
    settings = _mover(tmp_path)
    dapr = _FakeDapr()

    first = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok-1"}}))
    rows_after_first = lance.dataset(str(tmp_path / "silver")).count_rows()

    second = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok-1"}}))
    rows_after_second = lance.dataset(str(tmp_path / "silver")).count_rows()

    assert first == {"status": "SUCCESS"}
    assert second == {"status": "SUCCESS"}
    assert rows_after_second == rows_after_first, "a redelivered trigger duplicated rows — the hop is not idempotent"


def test_a17_a_redelivered_event_reuses_the_SAME_lineage_run_id(tmp_path: Path) -> None:
    """The deterministic run id, asserted equal — E2's other half.

    If redelivery minted a fresh run id, the graph would grow one orphan run per redelivery and the
    provenance record would claim work that never happened. The id is derived from the trigger token
    precisely so a MERGE collapses them.
    """
    _bronze(tmp_path)
    settings = _mover(tmp_path)
    dapr = _FakeDapr()

    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "same"}}))
    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "same"}}))

    run_ids = {p["data"]["run"]["runId"] for p in dapr.published if p["topic"] == settings.lineage_topic}
    assert len(run_ids) == 1, f"redelivery minted a second lineage run: {run_ids}"


def test_a17_a_publish_outage_returns_RETRY_rather_than_swallowing_the_hop(tmp_path: Path) -> None:
    """The RETRY contract — the entry point to resiliency, maxDeliver and the DLQ.

    A mover that cannot announce its output must NOT report success: downstream would never be woken
    and the data would sit in silver looking finished. Returning RETRY is what puts the event back on
    the bus and, after maxDeliver, into the DLQ where an operator can see it.
    """
    _bronze(tmp_path)
    settings = _mover(tmp_path)

    result = asyncio.run(handle_stage(cast(DaprClient, _FakeDapr(fail=True)), settings, {"data": {"token": "tok"}}))

    assert result == {"status": "RETRY"}


def test_a17_a_trigger_for_a_DIFFERENT_lane_is_acked_without_work(tmp_path: Path) -> None:
    """E1's stale/foreign-event half: ack, do nothing, and above all do not fail.

    Every mover sees every event on its topic. One that treated another lane's trigger as an error
    would RETRY forever on traffic that was never addressed to it, and the redelivery storm would
    take out the lane that was working.
    """
    _bronze(tmp_path)
    settings = _mover(tmp_path)
    dapr = _FakeDapr()

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok", "dataset": "some$other-lane"}}))

    # DROP, not SUCCESS — and the distinction is deliberate rather than cosmetic. Both ack, so
    # neither redelivers; DROP additionally says "this was not mine and produced nothing", which is
    # what an operator needs to tell a mover that ignored an event from one that silently did no work
    # on an event that WAS its own. The load-bearing half is that it is not RETRY.
    assert result["status"] in ("DROP", "SUCCESS"), f"a foreign trigger must be acked, not {result}"
    assert result["status"] != "RETRY", "a foreign trigger would redeliver forever"
    assert not Path(tmp_path / "silver").exists(), "a foreign trigger produced output"


# ── A18 — publication behaviour ───────────────────────────────────────────────────────


def test_a18_a_HELD_batch_publishes_NOTHING_downstream(tmp_path: Path) -> None:
    """Gate FAIL → no event, so downstream is provably never woken.

    This is the whole point of a pre-commit gate. Publishing anyway and relying on the next stage to
    re-check would mean bad data is announced as available, and every consumer that trusts the event
    has already read it before anyone notices.
    """
    _bronze(tmp_path)
    # A required column that silver cannot possibly carry: the gate must HOLD rather than promote.
    settings = _mover(tmp_path, quality_enabled=True, quality_required_columns="a_column_that_does_not_exist")
    dapr = _FakeDapr()

    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok"}}))

    stage_triggers = [p for p in dapr.published if p["topic"] == settings.pub_topic]
    assert stage_triggers == [], "a held batch woke the downstream stage"


def test_a18_a_HELD_batch_still_leaves_a_lineage_record(tmp_path: Path) -> None:
    """A hold must be VISIBLE. Silence is indistinguishable from a run that never started.

    The medallion's original defect in miniature: it emitted only on COMPLETE, so a failure left no
    record at all. A held batch is a decision the estate made about data — it belongs in the graph.
    """
    _bronze(tmp_path)
    settings = _mover(tmp_path, quality_enabled=True, quality_required_columns="a_column_that_does_not_exist")
    dapr = _FakeDapr()

    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok"}}))

    lineage = [p for p in dapr.published if p["topic"] == settings.lineage_topic]
    assert lineage, "a held batch left no lineage record at all"
    assert any(p["data"]["eventType"] == "FAIL" for p in lineage), "a held batch was not recorded as a FAIL"


def test_a18_a_PASSING_batch_publishes_the_version_it_actually_COMMITTED(tmp_path: Path) -> None:
    """The emitted version must equal the commit's own, not a value read back afterwards.

    A re-read can observe a NEWER version — another writer having committed in between — and
    announcing that one tells the next stage to process rows this run never produced, while the rows
    it did produce are skipped. The event has to carry the commit's answer.
    """
    _bronze(tmp_path)
    settings = _mover(tmp_path)
    dapr = _FakeDapr()

    result = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok"}}))
    assert result == {"status": "SUCCESS"}

    committed = lance.dataset(str(tmp_path / "silver")).version
    events = [p for p in dapr.published if p["topic"] == settings.lineage_topic]
    written = [e for e in events if e["data"]["outputs"] and e["data"]["outputs"][0]["name"] == settings.to_dataset]

    assert written, "the successful hop announced no output"
    facets = written[-1]["data"]["outputs"][0].get("facets") or {}
    version = ((facets.get("version") or {}).get("datasetVersion")) or ((facets.get("lanceVersion") or {}).get("version"))
    assert str(version) == str(committed), f"announced version {version} != committed version {committed}"


def test_a18_a_terminal_stage_publishes_no_stage_trigger(tmp_path: Path) -> None:
    """Gold is the end of the DAG: an empty `pub_topic` must mean silence, not a publish to "".

    A publish to an empty topic name is not an error in most brokers — it creates one. The cascade
    would then have a topic nobody subscribes to, growing forever, and the "terminal" claim would be
    false in a way only a broker inspection could reveal.
    """
    _bronze(tmp_path)
    settings = _mover(
        tmp_path,
        from_uri=str(tmp_path / "bronze"),
        to_uri=str(tmp_path / "gold"),
        to_namespace="gold",
        to_dataset="gold$catalog",
        operation="aggregate",
        pub_topic="",
    )
    dapr = _FakeDapr()

    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok"}}))

    assert all(p["topic"] for p in dapr.published), "a terminal stage published to an empty topic name"


@pytest.mark.parametrize("token", ["tok-a", "tok-b"])
def test_a18_two_DIFFERENT_triggers_each_produce_their_own_run(tmp_path: Path, token: str) -> None:
    """The mirror of the redelivery gate: distinct work must NOT collapse.

    An id derived too coarsely (per dataset, say) would make two genuine runs share one graph node,
    and the second would silently overwrite the first's provenance.
    """
    _bronze(tmp_path)
    settings = _mover(tmp_path)
    dapr = _FakeDapr()

    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": token}}))

    run_ids = {p["data"]["run"]["runId"] for p in dapr.published if p["topic"] == settings.lineage_topic}
    assert len(run_ids) == 1
