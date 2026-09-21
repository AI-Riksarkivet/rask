"""A `WorkOrder` must carry the one lineage fact no pod can hold: WHO the run reports as.

The Ray lane is the case that forces this. One head pod runs the train lane and all three stage
lanes, and each authenticates at the lineage ingest as its SUBMITTING stage runner's own subject —
`service-bronze-to-silver`, `service-silver-to-gold`, `service-media-to-silver`. A process env can
hold exactly one of those, so the identity has to ride the submission or the run reports as somebody
else. It also SELECTS the credential: `lineage-kit` picks `RASK_LINEAGE_TOKEN_<IDENTITY>` over the
shared token precisely so one pod can serve several subjects, and the head carries all four
(measured live 2026-09-21: `RASK_LINEAGE_TOKEN_SERVICE_{BRONZE_TO_SILVER,SILVER_TO_GOLD,
MEDIA_TO_SILVER,TRAINER}`). Name the wrong subject and the door answers 401 while the job writes its
data and exits SUCCEEDED — the failure shape `ServicePrincipal` records from 2026-07-13, where every
training RunEvent 401'd and the provenance vanished with one log line.

THE ENDPOINT IS THE OPPOSITE CASE AND IS ASSERTED ABSENT BELOW. Measured live 2026-09-21, the Ray
head's own process env already carries `RASK_LINEAGE_ENDPOINT=http://rask-lineage:8000` (rendered by
`lance.lineageEmitEnv`, `chart/templates/_ray-cluster-config.tpl:259`), which is `build_emitter`'s
FIRST alias choice, and the submitting stage runner would send the identical value
(`MEDALLION_STAGE_LINEAGE_URL=http://rask-lineage:8000`). It is one deployment fact about where this
cluster's lineage ingest is — the same shape as `S3_ENDPOINT`, which left the submission for the same
reason. Sending it too would give one value two owners, and Ray merges `runtime_env.env_vars` OVER
the worker's process env, so the submission would WIN — which is how a repointed pod keeps talking to
the old address and nothing says so.

DRIVEN THROUGH `lineage-kit`'s OWN PARSER rather than asserting key names. A test that checks
`"RASK_LINEAGE_SERVICE_IDENTITY" in env` passes while the consumer reads a name the producer never
sends; this estate has already paid for that gap once, when the model accepted two thirds of the Ray
lane's trio and not `LINEAGE_URL`, so the credential resolved, the endpoint did not, and the lane
degraded to the no-op that never raises. The producer and the consumer are checked against each
other, in one assertion.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.work_order import (
    WorkDestination,
    WorkIdentity,
    WorkOrder,
    WorkSource,
    WorkStamp,
    derive_idempotency_key,
)


SUBJECT = "service-bronze-to-silver"


def _order(**identity: str) -> WorkOrder:
    return WorkOrder(
        task="stage.bronze_to_silver",
        source=WorkSource(uri="s3://acme/bronze", table_id="acme-bronze$events"),
        destination=WorkDestination(uri="s3://acme/silver", table_id="acme-silver$events"),
        stamp=WorkStamp(stage="silver", cardinality="one_to_one"),
        identity=WorkIdentity(run_id="run-1", **identity),
        idempotency_key=derive_idempotency_key(stage="silver", token="t", from_uri="s3://acme/bronze", to_uri="s3://acme/silver", code_version="c"),
    )


def test_the_order_configures_the_subject_the_emitter_actually_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    """The producer's serialization and the consumer's parser, checked against each other."""
    from lineage_kit.config import LineageSettings

    for stale in ("RASK_LINEAGE_SERVICE_IDENTITY", "LINEAGE_SERVICE_ID"):
        monkeypatch.delenv(stale, raising=False)
    for key, value in _order(service_identity=SUBJECT).to_env().items():
        monkeypatch.setenv(key, value)

    assert LineageSettings().service_identity == SUBJECT, "the order names a reporting subject that `lineage-kit` does not resolve — the run reports as nobody"


def test_an_unwired_order_asserts_no_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitted, never blanked — the distinction `to_env` draws for every optional.

    A blank subject is not "no claim": `service_identity` selects the credential, so an empty string
    would send the run looking for `RASK_LINEAGE_TOKEN_` and fall back to the shared token, claiming
    a subject it has no key for. Absence leaves the pod's own configuration in place.
    """
    assert "RASK_LINEAGE_SERVICE_IDENTITY" not in _order().to_env()


def test_the_endpoint_is_the_pods_to_own_and_the_order_has_no_field_for_it() -> None:
    """`extra="forbid"` is what makes this a property of the type rather than a rule to remember."""
    with pytest.raises(Exception, match="extra_forbidden|Extra inputs"):
        WorkIdentity(run_id="run-1", lineage_endpoint="http://rask-lineage:8000")  # ty: ignore[unknown-argument]
    assert "RASK_LINEAGE_ENDPOINT" not in _order(service_identity=SUBJECT).to_env()


def test_the_ray_head_supplies_the_endpoint_the_submission_no_longer_carries() -> None:
    """The other half of the contract, and the reason this is a gate rather than a comment.

    Dropping `LINEAGE_URL` from the submission is only correct because the POD holds the endpoint. If
    `lance.lineageEmitEnv` ever leaves the Ray head, the stage lane loses its transport and the loss is
    SILENT BY CONSTRUCTION — `build_emitter` logs one warning and returns `NoopEmitter`, which drops
    every event at DEBUG, so a lane emitting into nothing is indistinguishable from outside from a lane
    that finished cleanly. The graph is simply empty and every job reports SUCCESS.

    The worker groups are counted in the same pass because the head's process env is what a job
    inherits. `workerGroupSpecs` is empty today, so every task runs on the head and the head's env is
    the whole story; the moment a worker group exists it needs this env too, and the assertion below
    will be measuring only half the cluster. Failing then is correct.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import chart_render

    heads = [
        (doc["metadata"]["name"], container, spec)
        for doc in chart_render.render("--set", "ray.enabled=true", "--set", "ray.cluster.enabled=true", "--set", "image.localImages=true")
        if doc.get("kind") in ("RayCluster", "RayService")
        for spec in [doc["spec"].get("rayClusterConfig", doc["spec"])]
        for container in spec["headGroupSpec"]["template"]["spec"]["containers"]
    ]
    assert heads, "no Ray head rendered — this gate would pass while asserting about nothing"

    for name, container, spec in heads:
        assert chart_render.env_of(container).get("RASK_LINEAGE_ENDPOINT"), (
            f"{name}/{container['name']} carries no RASK_LINEAGE_ENDPOINT, and the submission stopped sending one — the stage lane emits into a no-op"
        )
        assert not spec.get("workerGroupSpecs"), "a worker group exists and inherits nothing from the head's env — this gate now covers half the cluster"
