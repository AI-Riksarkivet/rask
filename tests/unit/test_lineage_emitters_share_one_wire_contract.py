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
