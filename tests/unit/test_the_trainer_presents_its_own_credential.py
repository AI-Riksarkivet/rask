"""The Ray head presents the TRAINER'S OWN credential, because that is what lineage's door demands.

`auth.dedicatedServiceCredentials` binds a privileged subject to `service-token-<identity>` at the
door (`service_kit.governed.dapr_auth.service_principal`): a privileged subject presenting the
SHARED `APP_API_TOKEN` is refused, deliberately and fail-closed, because one shared token across an
allowlist lets any holder claim the highest-privileged name on it.

The flag turns on BOTH lists. `services.yaml` renders `LINEAGE_PRIVILEGED_SUBJECTS` — the cascade
writers plus `medallion.train.trainerIdentity` — under exactly this flag, so the moment it is on,
`service-trainer` MUST present a dedicated token to reach the lineage ingest.

Every first-party service has that client half: `medallion.core.config.dedicated_token_for` reads
`service-token-<identity>` out of the Dapr secret store. The Ray head does not and cannot — it runs
no daprd, the same shape as the RustFS Tenant and the OpenFGA migrate Job — so it takes its secrets
by `secretKeyRef` off the infra-credentials Secret, which is the object `external-secrets.yaml`
already syncs from OpenBao. `ray-compute-access-key`/`-secret-key` are the precedent.

WHAT THIS COST, measured on the deployed estate 2026-09-06 with the flag on:

    lineage emit attempt 1 rejected: HTTP 401
    lineage emit attempt 2 rejected: HTTP 401        (x4 events)
    model models$churn published at registry version 28

Driving the door by hand with the head's own env returned `401 the presented credential may not
claim 'service-trainer'` — admitted subject, refused credential. Every training run published its
model and lost ALL of its provenance, and the job still exited SUCCEEDED, so nothing anywhere was
red. `test_governed_union_e2e::test_train_lineage_lands_attributed_under_governance` is the guard
that caught it, and its docstring names this exact failure as the reason it exists.

The head keeps the shared token when the flag is OFF: with no privileged list rendered, the trainer
is an ordinary subject and the shared token is the correct credential for one.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


TRAINER = "service-trainer"


def _head_env(*set_values: str) -> dict[str, dict]:
    docs = _rendered_docs("singleTenant.enabled=true", *set_values)
    services = [d for d in docs if d.get("kind") == "RayService"]
    assert services, "no RayService rendered"
    containers = services[0]["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"]
    return {e["name"]: e for c in containers for e in (c.get("env") or [])}


def _lineage_privileged(*set_values: str) -> str:
    docs = _rendered_docs("singleTenant.enabled=true", *set_values)
    for doc in docs:
        if doc.get("kind") != "Deployment" or "lineage" not in doc["metadata"]["name"]:
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if env["name"] == "LINEAGE_PRIVILEGED_SUBJECTS":
                    return str(env.get("value") or "")
    return ""


def test_the_trainer_is_privileged_at_lineage_when_the_flag_is_on() -> None:
    """The premise, asserted rather than assumed — if this stops being true the test below is moot."""
    assert TRAINER in _lineage_privileged("auth.dedicatedServiceCredentials=true").split(",")
    assert _lineage_privileged("auth.dedicatedServiceCredentials=false") == ""


def test_the_head_does_not_present_the_SHARED_token_for_a_privileged_trainer() -> None:
    """The defect: a privileged subject handed the one credential its door refuses by design."""
    entry = _head_env("auth.dedicatedServiceCredentials=true")["LINEAGE_SERVICE_TOKEN"]
    ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
    assert not (ref.get("name", "").endswith("-dapr-app-token") and ref.get("key") == "token"), (
        "the Ray head presents the SHARED app token while lineage requires `service-trainer`'s own — "
        "the door refuses it (`the presented credential may not claim 'service-trainer'`), so every "
        "training run loses all of its provenance while still exiting SUCCEEDED"
    )


def test_the_head_takes_the_trainers_dedicated_token_off_infra_credentials() -> None:
    """And it takes it the way every other daprd-less consumer does — no second source."""
    entry = _head_env("auth.dedicatedServiceCredentials=true")["LINEAGE_SERVICE_TOKEN"]
    assert "value" not in entry, f"the credential is a LITERAL in the rendered manifest: {entry}"
    ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
    assert ref.get("name", "").endswith("-infra-credentials"), f"not off infra-credentials: {ref}"
    assert ref.get("key") == f"service-token-{TRAINER}", f"wrong key: {ref}"


def test_infra_credentials_carries_the_token_the_head_asks_for() -> None:
    """A `secretKeyRef` at a key nothing writes is a fail-closed pod, not a fix."""
    docs = _rendered_docs("singleTenant.enabled=true", "auth.dedicatedServiceCredentials=true")
    secrets = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"].endswith("-infra-credentials")]
    assert secrets, "no infra-credentials Secret rendered"
    assert f"service-token-{TRAINER}" in (secrets[0].get("stringData") or {})


def test_the_seeded_token_and_the_mounted_token_are_THE_SAME_STRING() -> None:
    """The door compares them byte-for-byte, so two derivations that merely look alike is a 401.

    `openbao.yaml` seeds what the DOOR reads; infra-credentials holds what the HEAD sends. They are
    two templates, and the only thing making them agree is that both go through one helper.
    """
    docs = _rendered_docs("singleTenant.enabled=true", "auth.dedicatedServiceCredentials=true")
    secrets = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"].endswith("-infra-credentials")]
    mounted = (secrets[0].get("stringData") or {})[f"service-token-{TRAINER}"]

    seeds = [d for d in docs if d.get("kind") == "Job" and "openbao" in d["metadata"]["name"]]
    if not seeds:
        pytest.skip("no openbao seed Job rendered on this profile — nothing to compare against")
    script = str(seeds[0]["spec"]["template"]["spec"]["containers"][0])
    assert f"service-token-{TRAINER}={mounted}" in script, (
        "the token the head sends is not the token the seed writes — the door compares them with "
        "`secrets.compare_digest`, so anything but an exact match is a 401 nothing renders as an error"
    )
