"""The two job-side OpenLineage emitters must speak the SAME wire contract — pinned, since they
cannot share code.

open_ray-kernel.md move 4, the review's finding: `runners/dummy`'s hand-rolled emitter was "the only
cross-seal duplication in the tree with no pin". The seal is why it is a copy at all — the dummy
image builds from the runner's OWN lock, and its lineage module is deliberately stdlib-only so the
job depends on nothing — and a copy without a pin is where the estate's one-sided fixes land (the
credential echo; the work-axis id).

WHAT DRIFT COSTS HERE, and why each pinned item is load-bearing rather than cosmetic:

- **The `lance` facet's targeting keys** (`originator`, `project`) are the wire contract
  `notifiable()` reads. This is the sharpest edge in the estate's notification design
  (`rask-notifications`): coverage is decided at the PRODUCER, and an event whose targeting keys are
  misnamed is not under-delivered but UNDELIVERABLE — `notifiable()` answers it with a SUCCESS ack,
  so a renamed key in one emitter means that lane's failures reach nobody and nothing reports it.
- **`schemaURL`** is how the lineage ingest knows what it is parsing; two emitters on two spec
  revisions is a consumer bug nobody can see from either producer.
- **The DatasetVersion facet URL** is what lets the reconcile back-fill recover a version whose
  COMPLETE emit was lost.

Loaded BY PATH, not imported as packages: `scripts/` is not a member and `runners/dummy` is sealed —
but both modules are deliberately stdlib-only, which is what makes a behavioural pin possible at all
(the same trick `test_ray_stage_job.py` uses). If either ever grows a non-stdlib import, this pin
failing at load is the correct signal that the "self-contained job" premise changed.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType
from typing import Any


REPO = pathlib.Path(__file__).resolve().parents[2]


def _load(path: pathlib.Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


train = _load(REPO / "scripts" / "ray_train_job.py", "pin_train_job")
dummy = _load(REPO / "runners" / "dummy" / "src" / "dummy_runner" / "lineage.py", "pin_dummy_lineage")


def _events() -> tuple[dict[str, Any], dict[str, Any]]:
    train_event = train.build_event(
        event_type="COMPLETE",
        token="t1",
        model="m1",
        namespace="models",
        features=[],
        registry_uri="s3://models/registry",
        version=3,
        originator="user:alice",
        project="proj-a",
    )
    dummy_event = dummy.build_run_event(
        event_type="COMPLETE",
        run_id="00000000-0000-5000-8000-000000000001",
        to_id="silver$dummy",
        from_id="bronze$dummy",
        rows=5,
        version=3,
        originator="user:alice",
        project="proj-a",
    )
    return train_event, dummy_event


def test_both_emitters_stamp_the_same_runevent_spec() -> None:
    train_event, dummy_event = _events()
    assert train_event["schemaURL"] == dummy_event["schemaURL"], (
        "the two job-side emitters cite different RunEvent spec revisions — the lineage ingest is parsing two dialects"
    )


def test_both_emitters_target_people_through_the_same_facet_keys() -> None:
    """The notifiable() contract: `run.facets.lance.originator` + `.project`, exactly."""
    train_event, dummy_event = _events()
    for name, event in (("train", train_event), ("dummy", dummy_event)):
        lance = event["run"]["facets"].get("lance")
        assert lance is not None, f"{name}: no `lance` run facet — every targeting hint is gone and notifiable() acks the loss as SUCCESS"
        assert lance.get("originator") == "user:alice", f"{name}: the originator key drifted — this lane's runs reach nobody, silently"
        assert lance.get("project") == "proj-a", f"{name}: the project key drifted — project watchers never hear about this lane"


def test_both_emitters_pin_output_versions_with_the_same_facet() -> None:
    train_event, dummy_event = _events()

    def version_facet_url(event: dict[str, Any]) -> str:
        return str(event["outputs"][0]["facets"]["version"]["_schemaURL"])

    assert version_facet_url(train_event) == version_facet_url(dummy_event), (
        "the DatasetVersion facet URLs drifted — the reconcile back-fill recognises one lane's versions and not the other's"
    )


def test_a_role_literal_never_becomes_an_originator() -> None:
    """The medallion movers' live defect, which dummy's module exists partly to NOT reproduce: a
    role literal carried as originator writes into an inbox actor literally named `ray`."""
    event = dummy.build_run_event(
        event_type="COMPLETE",
        run_id="00000000-0000-5000-8000-000000000002",
        to_id="silver$dummy",
        from_id="bronze$dummy",
        originator="ray",
    )
    assert "originator" not in event["run"]["facets"]["lance"], "a role literal rode the originator key — an inbox actor named `ray` is about to exist"


def _emit_headers(module: ModuleType, env: dict[str, str], monkeypatch: Any) -> dict[str, str]:
    """The headers one emit would put on the wire, without sending anything.

    Both modules build `headers` and then hand it to `urllib.request.Request`, so intercepting the
    Request constructor is the only seam that does not require the emitter to be refactored for the
    test — and refactoring a SEALED runner to make it testable is the change this pin exists to avoid.
    """
    captured: dict[str, str] = {}

    class _Request:
        def __init__(self, url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> None:
            captured.update(headers or {})

    def _urlopen(*args: object, **kwargs: object) -> None:
        raise OSError("not sent — this pin inspects the headers, it does not reach the network")

    for key in ("LINEAGE_URL", "LINEAGE_SERVICE_TOKEN", "LINEAGE_SERVICE_ID", "LINEAGE_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module.urllib.request, "Request", _Request)
    monkeypatch.setattr(module.urllib.request, "urlopen", _urlopen)

    emit = getattr(module, "emit", None) or module.emit_event
    emit({"eventType": "COMPLETE"})
    return captured


def test_an_absent_service_id_never_becomes_an_empty_one(monkeypatch: Any) -> None:
    """A present-but-empty `x-lance-service-identity` is worse than an absent one, because the
    receiving door forks on PRESENCE.

    `services/lineage/src/lineage/api/security.py:164` reads
    `if dapr_api_token is not None and x_lance_service_identity is not None`, and `""` is not None —
    so an empty identity ASKS FOR the service door, and that branch is final: its own comment says
    "a refusal inside this branch is final and never re-asks OIDC". The emitters set the header
    unconditionally whenever `LINEAGE_SERVICE_TOKEN` is present, defaulting the id to `""`, and the
    `elif` then means a perfectly good `LINEAGE_TOKEN` bearer is never tried.

    The result is a job that does its work and loses its provenance: the run's rows land and its
    terminal event 403s, which is invisible from the job and from the graph alike.
    """
    for module, name in ((train, "scripts/ray_train_job.py"), (dummy, "runners/dummy/.../lineage.py")):
        headers = _emit_headers(
            module,
            {"LINEAGE_URL": "http://lineage:8000", "LINEAGE_SERVICE_TOKEN": "app-token", "LINEAGE_TOKEN": "a.valid.bearer"},
            monkeypatch,
        )
        assert headers.get("x-lance-service-identity") != "", (
            f"{name} sends an EMPTY service identity, which takes the service door with no subject and 403s"
        )
        assert "authorization" in headers, (
            f"{name} discarded a valid LINEAGE_TOKEN bearer while presenting no usable service identity"
        )


def test_a_named_service_id_still_takes_the_service_door(monkeypatch: Any) -> None:
    """The fix must not close the door it exists to open: with BOTH halves present the service
    identity is what goes on the wire, and no bearer is needed."""
    for module, name in ((train, "scripts/ray_train_job.py"), (dummy, "runners/dummy/.../lineage.py")):
        headers = _emit_headers(
            module,
            {"LINEAGE_URL": "http://lineage:8000", "LINEAGE_SERVICE_TOKEN": "app-token", "LINEAGE_SERVICE_ID": "service-trainer"},
            monkeypatch,
        )
        assert headers.get("dapr-api-token") == "app-token", name
        assert headers.get("x-lance-service-identity") == "service-trainer", name
