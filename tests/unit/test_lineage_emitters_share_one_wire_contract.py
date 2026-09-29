"""The two job-side OpenLineage emitters must speak the SAME wire contract.

One of them is now `lineage-kit`. `runners/dummy` was the hand-rolled copy this file was written to
pin — "the only cross-seal duplication in the tree with no pin" — and the seal was the argument for
it being a copy at all: the dummy image builds from the runner's own lock. That argument did not
survive measurement. `lineage-kit` is dependency-capped precisely so a sealed runner can take it as
a path dependency, the copy had already drifted two facet revisions, and the owner ruled
(2026-09-18, [[LIN-001]]) that the compute plane emits through the package. So dummy takes the
dependency and this file compares `scripts/ray_train_job.py` against the package instead.

THE PIN IS NOT LESS USEFUL FOR THAT — it is pointed at the remaining gap. `ray_train_job.py` is
baked into the Ray image, which carries `packages/ray-cluster-env` and NOT `lineage-kit`, so it
still hand-builds its envelope and its headers. These assertions are what will say, on the day that
changes, that nothing moved on the way across.

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
    ).to_wire()
    dummy_event = dummy.build_run_event(
        event_type="COMPLETE",
        run_id="00000000-0000-5000-8000-000000000001",
        to_id="silver$dummy",
        from_id="bronze$dummy",
        rows=5,
        version=3,
        originator="user:alice",
        project="proj-a",
    ).to_wire()
    return train_event, dummy_event


def test_both_emitters_target_people_through_the_same_facet_keys() -> None:
    """The notifiable() contract: `run.facets.lance.originator` + `.project`, exactly."""
    train_event, dummy_event = _events()
    for name, event in (("train", train_event), ("dummy", dummy_event)):
        lance = event["run"]["facets"].get("lance")
        assert lance is not None, f"{name}: no `lance` run facet — every targeting hint is gone and notifiable() acks the loss as SUCCESS"
        assert lance.get("originator") == "user:alice", f"{name}: the originator key drifted — this lane's runs reach nobody, silently"
        assert lance.get("project") == "proj-a", f"{name}: the project key drifted — project watchers never hear about this lane"


def _lineage_kit_headers(env: dict[str, str], monkeypatch: Any) -> dict[str, str]:
    """What `lineage_kit.build_emitter()` would actually send, for the same environment.

    Reads the built transport's own config — the seam `packages/lineage-kit/tests/test_config.py`
    uses — rather than re-deriving the rule, so this cannot pass while the emitter does something
    else. The bearer lives on the auth provider rather than in `custom_headers`, so it is folded in
    under the same key the hand-built emitter uses; that difference is transport plumbing, and the
    PROPERTY (a valid bearer is not discarded) is the same one either way.
    """
    from lineage_kit import build_emitter

    for key in ("LINEAGE_URL", "LINEAGE_SERVICE_TOKEN", "LINEAGE_SERVICE_ID", "LINEAGE_TOKEN", "RASK_LINEAGE_TOKEN_SERVICE_BRONZE_TO_SILVER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    emitter = build_emitter()
    config = emitter._client.transport.config  # ty: ignore[unresolved-attribute] — the package's own tests read it here
    headers = dict(config.custom_headers)
    if (bearer := getattr(config.auth, "get_bearer", lambda: None)()) is not None:
        headers["authorization"] = bearer
    return headers


def _emit_headers(module: ModuleType | None, env: dict[str, str], monkeypatch: Any) -> dict[str, str]:
    """The headers one emit would put on the wire, without sending anything.

    ``None`` selects `lineage-kit`, which every producer in the estate now emits through — including
        `scripts/ray_train_job.py`, converted 2026-09-19 (LIN-001). The module leg remains because the
        seam is still useful for anything that hand-builds headers, and there is nothing left that does.

        WHAT KEEPS THESE FOUR CASES ALIVE after the conversion is the ENVIRONMENT, not the emitter. Each
        one drives the package with the RAY TRAIN LANE's own variable spellings — `LINEAGE_URL`,
        `LINEAGE_SERVICE_TOKEN`, `LINEAGE_SERVICE_ID`, `RASK_LINEAGE_TOKEN_<IDENTITY>` — which are NOT
        `lineage-kit`'s canonical `RASK_LINEAGE_*` names. It accepts them through `AliasChoices`, and it
        once accepted two thirds of that trio and not `LINEAGE_URL`: the credential resolved, the endpoint
        did not, and the lane degraded to a silent no-op. That is the failure these keep shut.
    """
    if module is None:
        return _lineage_kit_headers(env, monkeypatch)
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
    # ONE SUBJECT NOW, and the env below is what keeps it from being a tautology: these are the RAY
    # TRAIN LANE's own variable spellings, not `lineage-kit`'s canonical `RASK_LINEAGE_*` ones. The
    # package accepts them through `AliasChoices`, and it once accepted two of the trio and not
    # `LINEAGE_URL` — credential resolving, endpoint not, degrading to a silent no-op.
    for module, name in ((None, "lineage-kit, driven with the Ray train lane's env"),):
        headers = _emit_headers(
            module,
            {"LINEAGE_URL": "http://lineage:8000", "LINEAGE_SERVICE_TOKEN": "app-token", "LINEAGE_TOKEN": "a.valid.bearer"},
            monkeypatch,
        )
        assert headers.get("x-lance-service-identity") != "", f"{name} sends an EMPTY service identity, which takes the service door with no subject and 403s"
        assert "authorization" in headers, f"{name} discarded a valid LINEAGE_TOKEN bearer while presenting no usable service identity"
