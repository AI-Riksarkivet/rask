"""The medallion plane mounts its OWN storage identity, and both halves of the pair move together.

Q17-5, the estate's one MISSING zero-trust control of nineteen. Measured on the live estate
2026-09-07, two consumers were already scoped and three were not:

    rask-maintenance          MAINTENANCE_S3_ACCESS_KEY_ID = rask-maintenance      scoped
    rask-medallion-producer   MEDALLION_S3_ACCESS_KEY_ID = minioadmin             ROOT
    the three stage runners' own     MEDALLION_S3_ACCESS_KEY_ID = minioadmin             ROOT

THE RAY LANE IS NOT A STAGE RUNNER ENV AND IS NOT THIS FILE'S SUBJECT. No credential rides `runtime_env` —
`ray_submit.py` says why: the Jobs API echoes it back on `GET /api/jobs/<id>`, an unauthenticated
dashboard published at the edge. The stage job therefore reads `S3_KEY`/`S3_SECRET` from the RAY
POD's own environment, which `chart/templates/rayservice.yaml` mounts by `secretKeyRef` off
infra-credentials. A stage runner env naming the Ray lane's key would bind to no setting and read as a
control while being decoration, so its ABSENCE from the assertions below is deliberate.

THE PAIR IS THE WHOLE TEST. `dapr_secret_s3_field` defaults to `minio-secret-key`, which IS the
tenant root's secret — so a scoped ACCESS KEY left on that default is signed with a mismatched pair
and every S3 call fails `SignatureDoesNotMatch`. Both the Ray and the maintenance pairs paid for that
once. A render that moves one half and not the other looks correct in a diff and takes the cascade
down on contact, which is exactly the class of failure a grep-shaped test cannot see.

THE DEFAULT IS THE SCOPED IDENTITY (2026-09-08). It was empty — the tenant root — and the reason was
ordering, not preference: `minio-scoped-users` ran `post-upgrade`, so a chart that named the key
rolled pods onto a credential the object store had not been told about yet. The hook now runs
`pre-upgrade`, so the user exists before the roll, and explicitly emptying the key is the deliberate
way back to root.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


SCOPED = ("minio.medallionAccessKey=rask-medallion", "minio.medallionSecretKey=d-secret")
#: The producer plus every stage runner — one identity, because `medallion.yaml` renders their S3 env from
#: one values pair and they do one class of work.
MEDALLION_DEPLOYMENTS = ("medallion-producer", "bronze-to-silver", "silver-to-gold")


def _medallion_envs(*set_values: str) -> dict[str, dict[str, str]]:
    """`{deployment name: {env name: value}}` for every Deployment carrying `MEDALLION_S3_ACCESS_KEY_ID`."""
    found: dict[str, dict[str, str]] = {}
    for doc in _rendered_docs(*set_values):
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            env = {e["name"]: e.get("value", "") for e in (container.get("env") or [])}
            if "MEDALLION_S3_ACCESS_KEY_ID" in env:
                found[doc["metadata"]["name"]] = env
    return found


def test_the_default_IS_the_provisioned_identity() -> None:
    """A default install must not present the tenant root for a user the same release provisions.

    This pinned the opposite until 2026-09-08, and said so honestly: "It is also the honest statement
    of where the estate stands — Q17-13 is the row for fixing the default." That row is done. The
    empty default's real defence was ordering — `minio-scoped-users` ran `post-upgrade`, so naming a
    key rolled pods onto a credential that did not exist yet — and the HOOK is what was wrong: it now
    runs `pre-upgrade` too.
    """
    envs = _medallion_envs()
    assert envs, "no medallion Deployment rendered"
    for name, env in envs.items():
        assert env["MEDALLION_S3_ACCESS_KEY_ID"] == "rask-medallion", f"{name} presents the tenant root on a default install"
        assert env.get("MEDALLION_DAPR_SECRET_S3_FIELD") == "medallion-s3-secret-key", (
            f"{name} carries the scoped key with the tenant root's secret field — every S3 operation signs wrong and fails SignatureDoesNotMatch"
        )


def test_every_medallion_deployment_takes_the_scoped_identity() -> None:
    """Not just the producer. A stage runner left on the root credential keeps the whole plane at root, since
    the credential a cascade writes with is whichever of them is widest."""
    envs = _medallion_envs(*SCOPED)
    covered = {name for name in envs if any(d in name for d in MEDALLION_DEPLOYMENTS)}
    assert len(covered) >= len(MEDALLION_DEPLOYMENTS), f"only {sorted(covered)} carry a medallion S3 identity"
    for name, env in envs.items():
        assert env["MEDALLION_S3_ACCESS_KEY_ID"] == "rask-medallion", f"{name} still runs as {env['MEDALLION_S3_ACCESS_KEY_ID']}"


@pytest.mark.parametrize("dapr", ["true", "false"])
def test_both_halves_of_the_pair_move_together(dapr: str) -> None:
    """THE FAILURE THIS EXISTS TO CATCH. With the secret store ON the secret is fetched by FIELD NAME,
    so the field has to move with the access key; with it OFF the secret is rendered inline and must be
    the scoped one. Either way, a scoped key paired with the root's secret fails every call."""
    envs = _medallion_envs(*SCOPED, f"dapr.secretsViaDapr={dapr}")
    assert envs, "no medallion Deployment rendered"
    for name, env in envs.items():
        assert env["MEDALLION_S3_ACCESS_KEY_ID"] == "rask-medallion", name
        if "MEDALLION_SECRETS_FROM_DAPR" in env:
            assert env.get("MEDALLION_DAPR_SECRET_S3_FIELD") == "medallion-s3-secret-key", (
                f"{name} reads the ROOT's secret field with a scoped access key — every S3 call fails SignatureDoesNotMatch"
            )
        else:
            assert env["MEDALLION_S3_SECRET_ACCESS_KEY"] == "d-secret", f"{name} renders the root secret beside a scoped key"


def test_the_secret_the_deployment_asks_for_is_the_one_openbao_seeds() -> None:
    """A field the service is told to read and nothing writes is a fail-closed boot: the medallion's
    `fetch_required_secrets` raises rather than degrading, so the pod never starts."""
    docs = _rendered_docs(*SCOPED)
    seeds = [d for d in docs if d.get("kind") == "Job" and "openbao" in d["metadata"]["name"]]
    if not seeds:
        pytest.skip("no openbao seed Job on this profile — nothing to compare against")
    assert "medallion-s3-secret-key=d-secret" in str(seeds[0]["spec"]["template"]["spec"]["containers"][0])
