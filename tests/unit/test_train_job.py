"""The Ray TRAIN job (#115b, docs/RAY-TRAIN.md D3+D4) — infra-free unit tier.

Covers the job's three contracts:
- LINEAGE (D3): spec-true RunEvents that round-trip through ``lineage.models.RunEvent`` — the official
  ``jobType=TRAINING`` facet, per-input version PINS, COMPLETE carries the registry version, FAIL keeps
  a BARE output + the standard ``errorMessage`` facet; run ids derive EXACTLY like the services' shared
  ``service_kit.openlineage.run_id_for`` (pinned so the two copies can never drift).
- REGISTRATION (D4): bytes FIRST (token-keyed, idempotent overwrite), then ONE Lance commit = the
  atomic registration; a crash between the two leaves orphan files but NEVER a half-registered model,
  and the token-keyed retry converges. Model version N == Lance version N.
- ORDER (via ``main``): START → RUNNING×N → COMPLETE, artifacts written before the registry commit.

The job is a standalone script baked into the ray image (no ``services/`` imports), so it is loaded
here by file path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import lance
import pyarrow as pa
import pytest

from lineage.models import RunEvent
from service_kit import openlineage as common_ol


_JOB_PATH = Path(__file__).parents[2] / "scripts" / "ray_train_job.py"


def _load_job() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ray_train_job", _JOB_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


job = _load_job()

_FEATURES = [{"dataset": "silver$features", "version": 7, "uri": "unused-here"}]


# --------------------------------------------------------------------------- #
# lineage shape (D3)
# --------------------------------------------------------------------------- #


def test_run_id_derivation_is_pinned_to_the_shared_helper() -> None:
    # The job mirrors service_kit.openlineage.run_id_for byte-for-byte (it cannot import services/). This
    # pin is what makes the mirror safe: any drift in namespace or algorithm fails here.
    for seed in ("train-abc123", "train-", "x"):
        assert job.run_id_for(seed) == common_ol.run_id_for(seed)


_REGISTRY = "s3://lake/medallion/models/churn"


def _event(**kw: Any) -> dict[str, Any]:
    return job.build_event(token="tok1", model="churn", namespace="models", features=_FEATURES, registry_uri=_REGISTRY, **kw)


def test_version_facet_spec_pin_matches_the_medallion_emitter() -> None:
    # The job cannot import services/, so its DatasetVersionDatasetFacet _schemaURL is a mirror —
    # pinned here against the medallion emitter's constant so the two can never drift (2026-07-11
    # review: the first draft hardcoded 1-0-0 while every sibling stamps 1-0-1).
    from medallion.schemas import events as medallion_events

    assert job._VERSION_FACET_SCHEMA == medallion_events._VERSION_FACET_SCHEMA
    # The rest of the deliberate self-contained mirror, pinned the same way (audit 2026-07-15: only the
    # version facet was pinned — the run/base/schema facet URLs could drift silently).
    assert job._RUN_SCHEMA == common_ol.RUN_EVENT_SCHEMA_URL
    assert job._BASE_FACET == common_ol.BASE_FACET_SCHEMA_URL
    complete = job.build_event(
        event_type="COMPLETE",
        token="pin",
        model="m",
        namespace="models",
        features=[{"dataset": "silver$f", "version": 1}],
        registry_uri="s3://x/m",
        version=1,
    )
    assert complete["outputs"][0]["facets"]["schema"]["_schemaURL"] == common_ol.SCHEMA_FACET_SCHEMA_URL
    # dataSource + errorMessage were the two mirrors still UNPINNED, and both had drifted a version
    # behind (1-0-0 while service_kit stamps 1-0-1) — the 1-0-0 documents even $ref the retired 1-0-2 core
    # spec while this event's envelope declares 2-0-2. Pinned now so the whole mirror is guarded.
    assert complete["outputs"][0]["facets"]["dataSource"]["_schemaURL"] == common_ol.DATASOURCE_FACET_SCHEMA_URL
    failed = job.build_event(
        event_type="FAIL",
        token="pin",
        model="m",
        namespace="models",
        features=[{"dataset": "silver$f", "version": 1}],
        registry_uri="s3://x/m",
        error="boom",
    )
    assert failed["run"]["facets"]["errorMessage"]["_schemaURL"] == common_ol.ERROR_MESSAGE_FACET_SCHEMA_URL


def test_start_event_carries_training_jobtype_and_input_pins() -> None:
    event = _event(event_type="START")
    parsed = RunEvent.model_validate(event)  # round-trips through the ingest model
    assert parsed.job.namespace == "ray-jobs" and parsed.job.name == "train.churn"
    job_type = parsed.job.facets["jobType"]
    assert (job_type["integration"], job_type["jobType"]) == ("RAY", "TRAINING")
    # the reproducibility key: every input surfaces its EXACT pinned Lance version
    assert parsed.inputs[0].name == "silver$features"
    assert parsed.inputs[0].facets["version"]["datasetVersion"] == "7"
    # START has no VERSION yet (nothing committed) — but dataSource (location metadata) is always
    # present so the reconcile back-fill can find the registry even if the COMPLETE emit is lost
    assert parsed.outputs[0].name == "models$churn"
    assert "version" not in parsed.outputs[0].facets
    assert parsed.outputs[0].source_uri == _REGISTRY
    assert parsed.run.run_id == common_ol.run_id_for("train-tok1")


def test_complete_event_carries_the_registry_version_and_schema() -> None:
    parsed = RunEvent.model_validate(_event(event_type="COMPLETE", version=3))
    out = parsed.outputs[0]
    assert out.facets["version"]["datasetVersion"] == "3"  # model version == Lance version
    assert [f.name for f in out.fields] == ["artifact", "payload", "meta"]
    assert out.source_uri == _REGISTRY


def test_fail_event_keeps_a_versionless_output_and_caps_the_error() -> None:
    parsed = RunEvent.model_validate(_event(event_type="FAIL", error="boom " * 400))
    assert "version" not in parsed.outputs[0].facets  # no fabricated version on failure
    assert parsed.outputs[0].source_uri == _REGISTRY  # location metadata is not a success claim
    message = parsed.run.facets["errorMessage"]["message"]
    assert len(message) == 1000  # capped like the other FAIL emitters


def test_progress_facet_rides_the_running_events() -> None:
    parsed = RunEvent.model_validate(_event(event_type="RUNNING", progress=(2, 3)))
    assert parsed.run.facets["progress"] == {
        "_producer": job._PRODUCER,
        "_schemaURL": job._BASE_FACET,
        "done": 2,
        "total": 3,
    }


# --------------------------------------------------------------------------- #
# registration (D4): bytes first, one atomic commit, token-keyed retry converges
# --------------------------------------------------------------------------- #


def test_write_artifacts_is_token_keyed_and_idempotent(tmp_path: Path) -> None:
    base = str(tmp_path / "artifacts")
    uris = job.write_artifacts(base, "tok1", {"weights.json": b"v1"})
    assert Path(uris["weights.json"]).read_bytes() == b"v1"
    assert f"{base}/tok1/" in uris["weights.json"] + "/"  # token-keyed path
    # a retried job overwrites ITS OWN paths — no duplicate objects, latest bytes win
    again = job.write_artifacts(base, "tok1", {"weights.json": b"v2"})
    assert again == uris and Path(uris["weights.json"]).read_bytes() == b"v2"


def test_publish_registry_creates_then_appends_and_returns_the_model_version(tmp_path: Path) -> None:
    registry = str(tmp_path / "registry")
    base = str(tmp_path / "artifacts")
    uris = job.write_artifacts(base, "tok1", {"weights.json": b"w", "metrics.json": b"m"})
    v1 = job.publish_registry(registry, base, uris, {"token": "tok1"}, None)
    assert v1 == 1  # first publish CREATES — model version 1 == Lance version 1
    v2 = job.publish_registry(registry, base, uris, {"token": "tok2"}, None)
    assert v2 == 2  # later publishes APPEND — full model history via time-travel
    ds = lance.dataset(registry)
    rows = ds.to_table(columns=["artifact", "meta"]).to_pylist()
    assert [r["artifact"] for r in rows] == ["metrics.json", "weights.json"] * 2  # sorted per publish
    assert json.loads(rows[0]["meta"]) == {"token": "tok1"}
    # time-travel: version 1 is exactly the first publish
    assert lance.dataset(registry, version=1).count_rows() == 2


def test_publish_registry_surfaces_the_real_append_error(tmp_path: Path) -> None:
    # Review 2026-07-11: a wider try/except misclassified append failures as "first publish" and
    # masked the real error behind 'Dataset already exists' from the fallback create. Pointers
    # OUTSIDE the registered external-blob base are the reproducer: the append itself must raise
    # its own error (which also documents the operational rule — the artifact base is stable per model).
    registry = str(tmp_path / "registry")
    base_a = str(tmp_path / "artifacts-a")
    base_b = str(tmp_path / "artifacts-b")
    uris_a = job.write_artifacts(base_a, "tok1", {"weights.json": b"w"})
    assert job.publish_registry(registry, base_a, uris_a, {}, None) == 1
    uris_b = job.write_artifacts(base_b, "tok2", {"weights.json": b"w"})
    with pytest.raises(OSError, match="(?i)external|base"):  # the REAL error, not 'already exists'
        job.publish_registry(registry, base_b, uris_b, {}, None)


def test_publish_registry_cas_race_loser_converges_as_append(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Simulate losing the concurrent first-create race: the existence probe says "missing" but the
    # create hits an already-existing dataset — the loser must converge as an append (version 2),
    # not terminally FAIL an expensive training run (D2: no auto-resubmit would ever repair it).
    registry = str(tmp_path / "registry")
    base = str(tmp_path / "artifacts")
    uris = job.write_artifacts(base, "tok1", {"weights.json": b"w"})
    assert job.publish_registry(registry, base, uris, {}, None) == 1

    def probe_races(uri: str, *a: Any, **kw: Any) -> Any:
        raise ValueError("simulated: dataset not found (probe raced the winner's create)")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lance, "dataset", probe_races)
        assert job.publish_registry(registry, base, uris, {}, None) == 2


def test_crash_between_bytes_and_commit_leaves_no_half_registration(tmp_path: Path) -> None:
    # D4's crash-window contract: artifacts landed, then the commit "crashed" → the registry does not
    # exist at all (orphan files only); the token-keyed retry re-lands the SAME paths and converges.
    registry = str(tmp_path / "registry")
    base = str(tmp_path / "artifacts")
    uris = job.write_artifacts(base, "tok1", {"weights.json": b"w"})
    with pytest.raises(ValueError):
        lance.dataset(registry)  # nothing half-registered
    retry_uris = job.write_artifacts(base, "tok1", {"weights.json": b"w"})
    assert retry_uris == uris
    assert job.publish_registry(registry, base, retry_uris, {"token": "tok1"}, None) == 1


# --------------------------------------------------------------------------- #
# main(): lifecycle order — bytes before commit, START → RUNNING×N → COMPLETE|FAIL
# --------------------------------------------------------------------------- #


def _run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, fail_publish: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    order: list[str] = []
    # Two writes so the PIN is exercised: the job must read version 1 (means 2.0), not LATEST (means 9.0).
    lance.write_dataset(pa.table({"amount": [1.0, 3.0]}), str(tmp_path / "silver"))
    lance.write_dataset(pa.table({"amount": [9.0]}), str(tmp_path / "silver"), mode="overwrite")
    features = [{"dataset": "silver$features", "version": 1, "uri": str(tmp_path / "silver")}]

    monkeypatch.setenv("MODEL", "churn")
    monkeypatch.setenv("TRAIN_TOKEN", "tok1")
    monkeypatch.setenv("FEATURES", json.dumps(features))
    monkeypatch.setenv("REGISTRY_URI", str(tmp_path / "registry"))
    monkeypatch.setenv("ARTIFACT_BASE", str(tmp_path / "artifacts"))
    monkeypatch.delenv("LINEAGE_URL", raising=False)
    monkeypatch.setattr(job, "emit", events.append)

    real_write, real_publish = job.write_artifacts, job.publish_registry

    def tracked_write(*a: Any, **kw: Any) -> dict[str, str]:
        order.append("bytes")
        return real_write(*a, **kw)

    def tracked_publish(*a: Any, **kw: Any) -> int:
        order.append("commit")
        if fail_publish:
            raise RuntimeError("commit refused")
        return real_publish(*a, **kw)

    monkeypatch.setattr(job, "write_artifacts", tracked_write)
    monkeypatch.setattr(job, "publish_registry", tracked_publish)
    if fail_publish:
        with pytest.raises(RuntimeError):
            job.main()
    else:
        job.main()
    return events, order


def test_main_trains_publishes_and_emits_the_full_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events, order = _run_main(monkeypatch, tmp_path)
    assert order == ["bytes", "commit"]  # D4: bytes BEFORE the atomic commit, exactly once each
    assert [e["eventType"] for e in events] == ["START", "RUNNING", "RUNNING", "COMPLETE"]
    complete = RunEvent.model_validate(events[-1])
    assert complete.outputs[0].facets["version"]["datasetVersion"] == "1"
    # the model trained on the PINNED version (means 2.0), not the floating LATEST (means 9.0)
    weights = json.loads(Path(tmp_path / "artifacts" / "tok1" / "weights.json").read_bytes())
    assert weights["weights"] == {"amount": 2.0}
    meta = json.loads(lance.dataset(str(tmp_path / "registry")).to_table(columns=["meta"]).to_pylist()[0]["meta"])
    assert meta["features"][0]["version"] == 1  # the pins are IN the registry record too


def test_main_emits_fail_and_reraises_when_the_commit_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events, order = _run_main(monkeypatch, tmp_path, fail_publish=True)
    assert order == ["bytes", "commit"]
    fail = RunEvent.model_validate(events[-1])
    assert fail.event_type == "FAIL" and "version" not in fail.outputs[0].facets
    assert fail.outputs[0].source_uri  # dataSource stays — location metadata, not a success claim
    assert "commit refused" in fail.run.facets["errorMessage"]["message"]


def test_main_emits_an_attributable_fail_on_misconfiguration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Review 2026-07-11: the consumer already acked (submit-and-ack) — if env parsing crashed
    # OUTSIDE the FAIL guard, a misconfigured run would vanish from provenance entirely.
    events: list[dict[str, Any]] = []
    monkeypatch.setenv("MODEL", "churn")
    monkeypatch.setenv("TRAIN_TOKEN", "tok1")
    monkeypatch.setenv("FEATURES", "{not json")
    monkeypatch.setenv("REGISTRY_URI", str(tmp_path / "registry"))
    monkeypatch.setenv("ARTIFACT_BASE", str(tmp_path / "artifacts"))
    monkeypatch.delenv("LINEAGE_URL", raising=False)
    monkeypatch.setattr(job, "emit", events.append)
    with pytest.raises(json.JSONDecodeError):
        job.main()
    assert [e["eventType"] for e in events] == ["FAIL"]
    fail = RunEvent.model_validate(events[0])
    assert fail.run.run_id == common_ol.run_id_for("train-tok1")  # attributable to the run
    assert "train config" in fail.run.facets["errorMessage"]["message"]


def test_emit_metrics_exports_numeric_metrics_as_otlp() -> None:
    # #18: a completed run's numeric metrics export to OTLP (→ GreptimeDB → Perses). An injected in-memory
    # reader captures without a real export; non-numeric metrics are skipped; a runs counter is emitted; the
    # only label is {model} (bounded cardinality).
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    job.emit_metrics("demo", {"rows_seen": 8, "features": 2, "note": "skip-me"}, reader=reader)

    data = reader.get_metrics_data()
    assert data is not None
    values: dict[str, float] = {}
    labels: dict[str, dict[str, object]] = {}
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for point in m.data.data_points:  # NumberDataPoint (counter/gauge) — value via getattr
                    values[m.name] = getattr(point, "value", 0)
                    labels[m.name] = dict((point.attributes or {}).items())

    assert values.keys() >= {"lance.training.rows_seen", "lance.training.features", "lance.training.runs"}
    assert "lance.training.note" not in values  # non-numeric metric is not chartable → skipped
    assert values["lance.training.rows_seen"] == 8
    assert labels["lance.training.rows_seen"] == {"lance.model": "demo"}  # bounded cardinality


def test_emit_metrics_is_a_noop_without_an_otlp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    # No endpoint configured (dev / auth-off) and no injected reader → a silent no-op, never raises.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    job.emit_metrics("demo", {"rows_seen": 8})
