"""Unit tests for the event-driven medallion movers + lance-ray producer.

Infra-free: no sidecar, no broker. A fake Dapr client records publishes; we pin the contract each
service must honor — the mover emits the transform's lineage (inputs→outputs) AND the next stage's
trigger, returns SUCCESS (RETRY on a publish outage), the producer emits ONLY the bronze-write event
(R23: bronze is the first governed tier — never a direct cascade publish), and the event-driven head
(/bronze-arrival) fires the medallion.bronze trigger for a bronze ingest while ignoring others.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, cast

import medallion.services.transform as mover
import pytest
from medallion.core.config import MedallionSettings
from medallion.schemas.events import build_run_event
from medallion.services.compute import WriteResult
from medallion.services.ingest_trigger import handle_bronze_arrival
from medallion.services.produce import produce

from service_kit.openlineage import run_id_for


class _FakeDapr:
    """Records publish_event calls; optionally fails to exercise the RETRY path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_: Any) -> None:
        if self._fail:
            raise RuntimeError("sidecar down")
        self.calls.append({"pubsub": pubsub_name, "topic": topic_name, "data": json.loads(data)})


class _AttemptDapr:
    """Records every publish ATTEMPT (parsed data) and fails attempts at/after ``fail_at`` — so a test can
    inspect what a best-effort FAIL emit tried to publish (``_FakeDapr`` raises before recording)."""

    def __init__(self, *, fail_at: int) -> None:
        self.attempts: list[dict[str, Any]] = []
        self._fail_at = fail_at

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_: Any) -> None:
        idx = len(self.attempts)
        self.attempts.append(json.loads(data))
        if idx >= self._fail_at:
            raise RuntimeError("sidecar down")


_BRONZE_TO_SILVER = MedallionSettings.model_validate(
    {
        "from_namespace": "bronze",
        "from_dataset": "bronze$events",
        "to_namespace": "silver",
        "to_dataset": "silver$features",
        "operation": "embed_features",
        "author": "data_eng",
        "sub_topic": "medallion.bronze",
        "pub_topic": "medallion.silver",
    }
)
# The same stage with the EVENT-DRIVEN Ray path on (compute + ray + from/to URIs). model_copy skips the
# validators, matching what the deployed pod resolves; a local path keeps storage_options() creds-free.
_RAY_MOVER = _BRONZE_TO_SILVER.model_copy(update={"compute_enabled": True, "ray_enabled": True, "from_uri": "/tmp/from", "to_uri": "/tmp/to"})


def test_build_run_event_records_the_transform_edge() -> None:
    event = build_run_event(
        operation="embed_features",
        author="data_eng",
        job_namespace="lance-medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="silver",
        output_name="silver$features",
        version=2,
        token="embed1",
    )
    assert event["inputs"][0]["name"] == "bronze$events"
    assert event["outputs"][0]["name"] == "silver$features"
    assert event["outputs"][0]["facets"]["version"]["datasetVersion"] == "2"
    assert event["run"]["facets"]["author"]["sub"] == "data_eng"
    assert event["job"]["name"] == "embed_features"
    # The standard sourceCodeLocation job facet — where the job's code lives (a here-dummy of what rask's
    # runner will auto-derive). type=git + the repo URL + the service path.
    source = event["job"]["facets"]["sourceCodeLocation"]
    assert source["type"] == "git"
    assert source["url"] == "https://github.com/Borg93/lance-ns"
    assert source["path"] == "services/medallion"
    assert "SourceCodeLocationJobFacet" in source["_schemaURL"]


def _run_event_for(project: str | None) -> dict[str, Any]:
    return build_run_event(
        operation="embed_features",
        author="data_eng",
        job_namespace="lance-medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="silver",
        output_name="silver$features",
        token="tok1",
        project=project,
    )


def test_run_id_without_project_keeps_the_single_tenant_seed() -> None:
    # Regression: a project-less emit derives its runId from EXACTLY the pre-#84 seed — the graph's
    # existing runs (and every redelivery of them) must keep MERGEing onto the same ids.
    assert _run_event_for(None)["run"]["runId"] == run_id_for("embed_features-tok1")


def test_run_id_is_project_qualified_so_tenants_never_collide() -> None:
    # Two projects reusing the SAME token must yield TWO distinct runs — an unqualified seed would
    # MERGE one tenant's run onto the other's, cross-wiring their lineage.
    acme, globex = _run_event_for("acme")["run"]["runId"], _run_event_for("globex")["run"]["runId"]
    assert acme == run_id_for("acme-embed_features-tok1")
    assert globex == run_id_for("globex-embed_features-tok1")
    assert acme != globex


def test_mover_emits_lineage_then_triggers_next_stage() -> None:
    dapr = _FakeDapr()
    event = {"data": {"token": "abc123", "dataset": "bronze$events", "namespace": "bronze"}}

    status = asyncio.run(mover.handle_stage(cast(Any, dapr), _BRONZE_TO_SILVER, event))

    assert status == {"status": "SUCCESS"}
    assert len(dapr.calls) == 2  # the lineage event, then the next-stage trigger
    lineage, trigger = dapr.calls
    assert lineage["topic"] == "lineage.events.v1"
    assert lineage["data"]["inputs"][0]["name"] == "bronze$events"
    assert lineage["data"]["outputs"][0]["name"] == "silver$features"
    # runId is a spec-valid UUID (deterministic on operation+token); the readable token rides the facet.
    assert lineage["data"]["run"]["runId"] == run_id_for("embed_features-abc123")
    assert lineage["data"]["run"]["facets"]["lance"]["token"] == "abc123"
    assert trigger["topic"] == "medallion.silver"
    assert trigger["data"] == {"token": "abc123", "dataset": "silver$features", "namespace": "silver"}


def test_mover_ray_branch_submits_job_then_emits_measured_lineage(monkeypatch: pytest.MonkeyPatch) -> None:
    """ray_enabled: handle_stage submits the Ray job, measures the written dataset, and emits the SAME
    lineage (measured version AND column edges) + triggers the next stage — the in-process contract, via Ray.

    The Ray branch must read back with ``measure_stage``, not a bare ``measure``: the transform happened
    out-of-process, so the column edges are reconstructed from the on-disk schemas. A bare measure would
    hand ``build_run_event`` an empty column_map and the columnLineage facet would silently vanish on the
    production seam (the real end-to-end proof is in tests/unit/test_column_lineage_emit.py)."""
    from medallion.services.compute import WriteResult

    submitted: dict[str, Any] = {}

    async def fake_submit(_settings: Any, *, from_uri: str, to_uri: str, stage: str, token: str) -> None:
        submitted.update({"from": from_uri, "to": to_uri, "stage": stage, "token": token})

    monkeypatch.setattr(mover, "submit_stage_job", fake_submit)
    measured = WriteResult(version=7, row_count=5, size_bytes=99, column_map=[("id", "id", "IDENTITY")])
    measured_uris: dict[str, str] = {}

    def fake_measure_stage(from_uri: str, to_uri: str, _so: dict[str, str]) -> WriteResult:
        measured_uris.update({"from": from_uri, "to": to_uri})
        return measured

    monkeypatch.setattr(mover, "measure_stage", fake_measure_stage)
    dapr = _FakeDapr()

    status = asyncio.run(mover.handle_stage(cast(Any, dapr), _RAY_MOVER, {"data": {"token": "tok"}}))

    assert status == {"status": "SUCCESS"}
    assert submitted == {"from": "/tmp/from", "to": "/tmp/to", "stage": "silver", "token": "tok"}
    # The measure reads BOTH ends — it needs the upstream schema to reconstruct the edges.
    assert measured_uris == {"from": "/tmp/from", "to": "/tmp/to"}
    lineage, trigger = dapr.calls  # emit then next-stage trigger
    facets = lineage["data"]["outputs"][0]["facets"]
    assert facets["version"]["datasetVersion"] == "7"  # the measured version
    assert facets["columnLineage"]["fields"]["id"]["inputFields"][0]["field"] == "id"
    assert trigger["topic"] == "medallion.silver"


def test_mover_ray_branch_retries_when_job_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from medallion.services.ray_submit import RayJobError

    async def fake_submit(*_a: Any, **_k: Any) -> None:
        raise RayJobError("ray job FAILED")

    monkeypatch.setattr(mover, "submit_stage_job", fake_submit)
    status = asyncio.run(mover.handle_stage(cast(Any, _FakeDapr()), _RAY_MOVER, {"data": {"token": "t"}}))
    assert status == {"status": "RETRY"}  # a failed Ray job → RETRY → Dapr redelivers (re-attaches)


def test_mover_write_is_single_flight_under_concurrent_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent deliveries of the SAME stage serialize their Lance write (item 7). Without the
    process-wide _write_lock both would enter the write (two threads → two `mode="overwrite"` commits
    racing on the same target); with it, at most ONE is ever in the critical section."""
    settings = _BRONZE_TO_SILVER.model_copy(update={"compute_enabled": True, "from_uri": "/tmp/f", "to_uri": "/tmp/t"})
    active = 0
    max_active = 0

    def slow_transform(*_a: Any, **_k: Any) -> WriteResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.05)  # hold the critical section so an unguarded second write WOULD overlap
        active -= 1
        return WriteResult(version=1, row_count=1, size_bytes=1)

    monkeypatch.setattr(mover, "transform_stage", slow_transform)

    async def _run_two() -> list[dict[str, str]]:
        return list(
            await asyncio.gather(
                mover.handle_stage(cast(Any, _FakeDapr()), settings, {"data": {"token": "a"}}),
                mover.handle_stage(cast(Any, _FakeDapr()), settings, {"data": {"token": "b"}}),
            )
        )

    results = asyncio.run(_run_two())
    assert all(r == {"status": "SUCCESS"} for r in results)
    assert max_active == 1  # serialized — never two writes in flight for the same target at once


def test_ray_mover_submits_for_blob_upstreams(monkeypatch: pytest.MonkeyPatch) -> None:
    """ray_enabled + a blob-carrying upstream now goes to the RAY job (Phase-3 parity, 2026-07-13): the
    stage job round-trips the blob column via pylance (read_blobs → blob_array → 2.2 write) and derives
    thumbnail+embedding — so there is no in-process fallback anymore. The old fallback is GONE."""
    from medallion.services.compute import WriteResult

    measured = WriteResult(version=7, row_count=5, size_bytes=99)
    monkeypatch.setattr(mover, "measure_stage", lambda _from, _to, _so: measured)
    submitted: list[str] = []

    async def fake_submit(*_a: Any, **_k: Any) -> None:
        submitted.append("ray")

    monkeypatch.setattr(mover, "submit_stage_job", fake_submit)
    transformed: list[str] = []

    def fake_transform(_f: str, _t: str, _so: dict[str, str], *, stage: str) -> WriteResult:
        transformed.append(stage)
        return WriteResult(version=2, row_count=1, size_bytes=10)

    monkeypatch.setattr(mover, "transform_stage", fake_transform)
    dapr = _FakeDapr()

    status = asyncio.run(mover.handle_stage(cast(Any, dapr), _RAY_MOVER, {"data": {"token": "tok"}}))

    assert status == {"status": "SUCCESS"}
    assert submitted == ["ray"]  # the Ray job WAS submitted — blobs no longer force in-process
    assert transformed == []  # in-process transform did NOT run


def test_terminal_mover_emits_lineage_but_no_next_trigger() -> None:
    terminal = _BRONZE_TO_SILVER.model_copy(update={"pub_topic": ""})  # gold: no downstream
    dapr = _FakeDapr()

    status = asyncio.run(mover.handle_stage(cast(Any, dapr), terminal, {"data": {"token": "t"}}))

    assert status == {"status": "SUCCESS"}
    assert len(dapr.calls) == 1 and dapr.calls[0]["topic"] == "lineage.events.v1"


def test_medallion_apps_build_their_openapi() -> None:
    """Regression: the FastAPI apps must construct AND build their OpenAPI schema.

    A route whose return annotation isn't a valid Pydantic response model (e.g. ``dict | JSONResponse``)
    passes every service-level test but crashes the app at startup/`openapi()` — this pins that the
    producer + mover apps actually stand up. (Caught live when /produce's RFC-9457 union broke lance-ray.)"""
    import medallion.mover as mover_app
    import medallion.producer as producer_app

    assert producer_app.app.openapi()["openapi"]  # the crash path — must not raise
    assert mover_app.app.openapi()["openapi"]


def test_mover_retries_on_publish_failure() -> None:
    status = asyncio.run(mover.handle_stage(cast(Any, _FakeDapr(fail=True)), _BRONZE_TO_SILVER, {"data": {"token": "t"}}))
    assert status == {"status": "RETRY"}


def test_producer_emits_only_the_bronze_write_event() -> None:
    # Event-driven head (B2, re-tiered by R23): produce() emits ONLY the bronze-write lineage event — it
    # never publishes the medallion.bronze trigger. /bronze-arrival reacts to this event and fires it.
    dapr = _FakeDapr()

    result = asyncio.run(produce(cast(Any, dapr), MedallionSettings()))

    assert result["status"] == "produced"
    assert len(dapr.calls) == 1
    (bronze_lineage,) = dapr.calls
    assert bronze_lineage["topic"] == "lineage.events.v1"
    assert bronze_lineage["data"]["outputs"][0]["name"] == "bronze$events"
    assert bronze_lineage["data"]["outputs"][0]["namespace"] == "bronze"
    assert bronze_lineage["data"]["inputs"] == []  # the dummy seed has no external source
    assert all(c["topic"] != "medallion.bronze" for c in dapr.calls)  # the trigger is the subscription's job


def test_iiif_producer_emits_only_the_bronze_write_event(monkeypatch: pytest.MonkeyPatch) -> None:
    # The P7a IIIF twin of the pin above (R23 event-shape proof): ingest_iiif() emits ONLY the
    # bronze-write lineage event — input is the EXTERNAL iiif://… source, output the bronze dataset —
    # and it never publishes the medallion.bronze trigger itself (the /bronze-arrival subscription fires
    # the cascade off the event; publishing both would double-fire it).
    import medallion.services.iiif_produce as iiif_mod
    from medallion.services.iiif_produce import ingest_iiif
    from medallion.services.ingest import IngestResult

    monkeypatch.setattr(
        iiif_mod,
        "_harvest_and_ingest",
        lambda *_a: IngestResult(version=2, row_count=3, source_uris=[], fields=[{"name": "payload", "type": "blob"}]),
    )
    dapr = _FakeDapr()
    settings = MedallionSettings.model_validate({"compute_enabled": True, "iiif_bronze_uri": "memory://bronze"})

    result = asyncio.run(ingest_iiif(cast(Any, dapr), settings, "A0068688", token="tok9"))

    assert result["status"] == "ingested"
    assert len(dapr.calls) == 1
    (bronze_lineage,) = dapr.calls
    assert bronze_lineage["topic"] == "lineage.events.v1"
    assert bronze_lineage["data"]["outputs"][0] == {
        "namespace": "bronze",
        "name": "bronze$pages",
        "facets": bronze_lineage["data"]["outputs"][0]["facets"],  # facet content asserted below
    }
    # ONE external-source input: the iiif:// manifest (scheme://authority namespace + volume-id name).
    assert bronze_lineage["data"]["inputs"] == [{"namespace": "iiif://iiifintern-ai.ra.se", "name": "A0068688"}]
    assert bronze_lineage["data"]["run"]["runId"] == run_id_for("iiif-ingest-tok9")  # deterministic → redelivery MERGEs
    assert bronze_lineage["data"]["outputs"][0]["facets"]["schema"]["fields"] == [{"name": "payload", "type": "blob"}]
    assert all(c["topic"] != "medallion.bronze" for c in dapr.calls)  # the trigger is the subscription's job


def test_bronze_arrival_fires_the_cascade_for_the_page_lane() -> None:
    # The IIIF page lane shares the cascade head: a COMPLETE write to (bronze, bronze$pages) fires
    # medallion.bronze with the PAGE dataset named on the trigger, so the mover knows which lane arrived.
    dapr = _FakeDapr()
    event = {
        "data": build_run_event(
            operation="iiif-ingest",
            author="ray",
            job_namespace="lance-medallion",
            inputs=[("iiif://iiifintern-ai.ra.se", "A0068688")],
            output_namespace="bronze",
            output_name="bronze$pages",
            version=2,
            token="tok77",
        )
    }

    status = asyncio.run(handle_bronze_arrival(cast(Any, dapr), MedallionSettings(), event))

    assert status == {"status": "SUCCESS"}
    (trigger,) = dapr.calls
    assert trigger["topic"] == "medallion.bronze"
    assert trigger["data"] == {"token": "tok77", "dataset": "bronze$pages", "namespace": "bronze"}


def _bronze_write_cloudevent(token: str = "tok123") -> dict[str, Any]:
    """A Dapr CloudEvent whose data is a bronze-dataset write (what /bronze-arrival reacts to)."""
    return {
        "data": build_run_event(
            operation="lance_ray_ingest",
            author="ray",
            job_namespace="lance-medallion",
            inputs=[],
            output_namespace="bronze",
            output_name="bronze$events",
            version=1,
            token=token,
        )
    }


def test_bronze_arrival_fires_the_cascade() -> None:
    # A bronze-dataset write event drives the head: publish the medallion.bronze trigger (event-driven B2).
    dapr = _FakeDapr()

    status = asyncio.run(handle_bronze_arrival(cast(Any, dapr), MedallionSettings(), _bronze_write_cloudevent()))

    assert status == {"status": "SUCCESS"}
    assert len(dapr.calls) == 1
    (trigger,) = dapr.calls
    assert trigger["topic"] == "medallion.bronze"
    assert trigger["data"] == {
        "token": "tok123",  # threaded from the bronze event's lance.token facet, not its (now-UUID) runId
        "dataset": "bronze$events",
        "namespace": "bronze",
    }


def test_bronze_arrival_ignores_non_bronze_event() -> None:
    # Loop guard: a mover's silver write on the SAME topic is acked and drives nothing — the head can't
    # self-trigger off the cascade it started.
    dapr = _FakeDapr()
    silver = {
        "data": build_run_event(
            operation="embed_features",
            author="data_eng",
            job_namespace="lance-medallion",
            inputs=[("bronze", "bronze$events")],
            output_namespace="silver",
            output_name="silver$features",
            version=1,
            token="tok456",
        )
    }

    status = asyncio.run(handle_bronze_arrival(cast(Any, dapr), MedallionSettings(), silver))

    assert status == {"status": "SUCCESS"}
    assert dapr.calls == []  # nothing published


def test_bronze_arrival_retries_on_publish_failure() -> None:
    status = asyncio.run(handle_bronze_arrival(cast(Any, _FakeDapr(fail=True)), MedallionSettings(), _bronze_write_cloudevent()))
    assert status == {"status": "RETRY"}


def test_compute_with_s3_endpoint_but_no_secret_fails_fast() -> None:
    # A genuinely credential-less compute deploy (no plaintext secret AND no store path) → fail at config
    # load with a clear message, not at first write with a cryptic S3 SignatureDoesNotMatch.
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=r"no\s+MEDALLION_SECRETS_FROM_DAPR"):
        MedallionSettings.model_validate({"compute_enabled": True, "s3_endpoint": "http://rustfs:9000", "s3_secret_access_key": ""})


def test_compute_with_dapr_secret_store_boots_without_plaintext_secret() -> None:
    # CONTRACT (audit 2026-07-15): the chart's compute+OpenBao combo withholds the plaintext secret and
    # sets MEDALLION_SECRETS_FROM_DAPR — the guard must NOT crash it; the lifespan's fail-closed
    # apply_dapr_secrets is the enforcement point for the store path (same shape as catalog/lineage).
    settings = MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "s3_endpoint": "http://rustfs:9000",
            "s3_secret_access_key": "",
            "secrets_from_dapr": True,
        }
    )
    assert settings.secrets_from_dapr is True


async def _allow(*_a: Any, **_k: Any) -> bool:
    return True


async def _deny(*_a: Any, **_k: Any) -> bool:
    return False


def test_mover_denied_when_not_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    # FGA gate on + the service identity lacks the required role → DROP, and NOTHING is published.
    monkeypatch.setattr(mover.fga, "check", _deny)
    dapr = _FakeDapr()
    status = asyncio.run(mover.handle_stage(cast(Any, dapr), _BRONZE_TO_SILVER, {"data": {"token": "t"}}, fga_client=cast(Any, object())))
    assert status == {"status": "DROP"}
    assert dapr.calls == []  # not authorized → no lineage emitted, no next stage triggered


def test_mover_allowed_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mover.fga, "check", _allow)
    dapr = _FakeDapr()
    status = asyncio.run(mover.handle_stage(cast(Any, dapr), _BRONZE_TO_SILVER, {"data": {"token": "t"}}, fga_client=cast(Any, object())))
    assert status == {"status": "SUCCESS"}
    assert len(dapr.calls) == 2  # authorized → lineage + next trigger


def test_mover_retries_on_fga_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    # An FGA OUTAGE (fail-closed 503 from fga.check) is transient — unlike a denial: the mover must
    # return the explicit RETRY contract so the sidecar redelivers, and publish NOTHING meanwhile.
    from lance_namespace import ServiceUnavailableError

    async def _outage(*_a: Any, **_k: Any) -> bool:
        raise ServiceUnavailableError("openfga unreachable")

    monkeypatch.setattr(mover.fga, "check", _outage)
    dapr = _FakeDapr()
    status = asyncio.run(mover.handle_stage(cast(Any, dapr), _BRONZE_TO_SILVER, {"data": {"token": "t"}}, fga_client=cast(Any, object())))
    assert status == {"status": "RETRY"}
    assert dapr.calls == []  # nothing emitted while authz is unanswerable


def test_mover_emits_fail_event_on_transform_failure() -> None:
    # A genuine transform failure (the FIRST publish — the COMPLETE emit — fails) records a FAIL RunEvent:
    # it keeps a BARE output (so the lineage repo makes a WROTE edge → producers() surfaces the attempt)
    # with NO version facet, carries the standard errorMessage facet, and returns RETRY.
    dapr = _AttemptDapr(fail_at=0)  # the COMPLETE emit fails
    status = asyncio.run(mover.handle_stage(cast(Any, dapr), _BRONZE_TO_SILVER, {"data": {"token": "t"}}))
    assert status == {"status": "RETRY"}
    # Two ATTEMPTS: the COMPLETE (failed), then the best-effort FAIL.
    fail_events = [e for e in dapr.attempts if e.get("eventType") == "FAIL"]
    assert fail_events, "a transform failure must emit a FAIL RunEvent"
    fail = fail_events[-1]
    assert fail["outputs"][0]["name"] == "silver$features"  # bare output → WROTE edge for producers()
    assert "version" not in fail["outputs"][0].get("facets", {})  # a failed run asserts no version
    assert fail["run"]["facets"]["errorMessage"]["message"] == "sidecar down"


def test_mover_does_not_fail_run_when_only_the_trigger_publish_fails() -> None:
    # CONTRACT (review of 73af2fd): if the COMPLETE emit SUCCEEDS but the downstream trigger publish fails,
    # the run completed — it must NOT be flipped to FAIL. Only RETRY (redelivery re-emits the idempotent
    # COMPLETE + re-publishes the trigger). fail_at=1 → the COMPLETE lands, the trigger publish raises.
    dapr = _AttemptDapr(fail_at=1)
    status = asyncio.run(mover.handle_stage(cast(Any, dapr), _BRONZE_TO_SILVER, {"data": {"token": "t"}}))
    assert status == {"status": "RETRY"}
    # Attempts are exactly COMPLETE then the trigger — NO FAIL event (no spurious FAIL for a done run).
    assert [e.get("eventType", "trigger") for e in dapr.attempts] == ["COMPLETE", "trigger"]
    assert not any(e.get("eventType") == "FAIL" for e in dapr.attempts)
