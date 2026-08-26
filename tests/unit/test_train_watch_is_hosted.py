"""The producer must host a workflow runtime whenever it hosts a WORKFLOW — not only for reviews.

`schedule_train_watch` starts `train_run` on the producer, and `train_run` is registered by the same
`register()` the producer's lifespan calls. But that lifespan started a runtime only when
`quality_review_enabled` was on, and the chart defaults `medallion.qualityReview` FALSE while
`medallion.ray` defaults TRUE. So on the default chart every training job was submitted and then
never watched: `DaprWorkflowClient()` fails, `schedule_train_watch` logs
`medallion_train_watch_not_scheduled` and returns None, and the trigger ACKS.

That failure is silent by design on this lane, which is what makes it dangerous. The docstring is
right that a lost watcher must not fail the trigger -- the job is already running, and retrying would
re-enter the FGA gate and a submit that refuses to resubmit. So nobody is ever told: no terminal
event, no `report_train_outcome`, no notification to the originator whose four-hour run finished.

The Dapr statestore scope carried the SAME gate, with a comment reasoning it out explicitly from the
lifespan's condition -- correct about the lifespan, and wrong about which features need a runtime.

Owner ruling (2026-08-25): gate both on `qualityReview OR ray`. The producer hosts `promotion_review`
(needs qualityReview) and `train_run` (needs ray), so its runtime starts when EITHER is on and stays
off when neither is -- rather than running an actor-backed engine in every deployment.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"

#: The producer's app-id as the chart names it.
PRODUCER_APP_ID = "medallion-producer"


def _helm(*set_values: str) -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(CHART)]
    argv += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    argv += ["--set", "image.localImages=true"]
    for value in set_values:
        argv += ["--set", value]
    done = subprocess.run(argv, capture_output=True, text=True, check=True)  # noqa: S603
    return done.stdout


def _actor_statestore_scopes(rendered: str) -> list[str]:
    """The scopes of the Dapr component that carries the ACTOR state store capability.

    Read off the rendered manifest rather than grepped, so a scope list that moves under a different
    component does not quietly pass.
    """
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "Component":
            continue
        meta = doc.get("metadata") or {}
        spec = doc.get("spec") or {}
        is_actor_store = any(str(item.get("name")) == "actorStateStore" and str(item.get("value")).lower() == "true" for item in (spec.get("metadata") or []))
        if is_actor_store:
            return [str(s) for s in (doc.get("scopes") or [])]
        # Some charts declare the capability only through the component's own name.
        if "statestore" in str(meta.get("name", "")) and doc.get("scopes"):
            return [str(s) for s in doc["scopes"]]
    return []


def test_the_chart_DEFAULTS_scope_the_producer_onto_the_actor_state_store() -> None:
    """THE WEDGE, as the default chart ships it: ray on, quality review off.

    Unscoped, the sidecar still logs "Workflow engine started" and every schedule call fails -- the
    invisible failure `reference-dapr-actor-state-store-silent` records.
    """
    scopes = _actor_statestore_scopes(_helm())

    assert PRODUCER_APP_ID in scopes, f"the producer hosts train_run on the DEFAULT chart but is not scoped onto the actor state store; scopes were {scopes}"


def test_ray_alone_is_enough_to_scope_the_producer() -> None:
    scopes = _actor_statestore_scopes(_helm("medallion.ray=true", "medallion.qualityReview=false"))

    assert PRODUCER_APP_ID in scopes


def test_quality_review_alone_is_STILL_enough() -> None:
    """The half that already worked must keep working."""
    scopes = _actor_statestore_scopes(_helm("medallion.ray=false", "medallion.qualityReview=true"))

    assert PRODUCER_APP_ID in scopes


def test_NEITHER_feature_leaves_the_producer_unscoped() -> None:
    """The ruling was `qualityReview OR ray`, not "always". A deployment using neither feature hosts
    no workflow, so scoping it would grant an actor state store nothing asks for."""
    scopes = _actor_statestore_scopes(_helm("medallion.ray=false", "medallion.qualityReview=false"))

    assert PRODUCER_APP_ID not in scopes


def test_the_lifespan_condition_and_the_chart_gate_AGREE() -> None:
    """The two halves are written in different languages in different files, and the statestore
    comment derives its condition from the lifespan's. When they drift, the sidecar says nothing.

    Asserted as source text because there is no runtime seam that exposes both at once.
    """
    lifespan = (REPO / "services/medallion/src/medallion/producer.py").read_text()
    template = (REPO / "chart/templates/dapr-statestore.yaml").read_text()

    assert re.search(r"if\s+.*quality_review_enabled\s+or\s+.*ray_enabled", lifespan), "the producer lifespan does not start a runtime for the RAY feature"
    assert re.search(r"if\s+or\s+\.Values\.medallion\.qualityReview\s+\.Values\.medallion\.ray", template), (
        "the statestore scope gate does not match the lifespan condition"
    )
