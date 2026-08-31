"""The Ray plane's S3 credential comes from the estate's own secret object, not a hand-made one.

WHY IT IS NOT READ FROM DAPR, since that is the obvious question. A Ray pod has NO daprd — recorded
in `.claude/skills/rask-dapr`'s sidecar map alongside the web zones and the runners — so a job cannot
call `/v1.0/secrets/*`. That is a fact about the deployment, not a preference, and the same skill
names the sanctioned alternative for exactly this case: a pod that must hold a secret and has no
sidecar gets it through `secretKeyRef` onto a Secret the estate already owns, which
`external-secrets.yaml` syncs FROM OpenBao on the prod path.

So the rule this gate keeps is the ONE-SOURCE rule, not a no-Dapr rule: the credential is a key on
`infra-credentials` — the same object that already carries `rustfs-secret-key` — so the dev Secret and
the ESO sync agree by construction. A second Secret invented beside it would be a second source, and
on the prod path it would be the one ESO does not fill.

`rayservice.yaml` states the other half of the reason: a literal `value:` would put the credential in
`helm get manifest`.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml


REPO = pathlib.Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _render(*sets: str) -> str:
    import shutil
    import subprocess

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm,
        "template",
        str(CHART),
        "--set",
        "image.localImages=true",
        "--set",
        "frontend.oidc.sessionSecret=probe-secret-that-is-long-enough-x",
        "--set-string",
        "frontend.oidc.publicIssuer=http://localhost:8080/dex",
        "--set-string",
        "frontend.oidc.publicOrigin=http://localhost:8080",
        "--set-string",
        "dex.issuer=http://localhost:8080/dex",
        *[a for s in sets for a in ("--set", s)],
    ]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def _secret_keys(rendered: str, name_fragment: str) -> set[str]:
    keys: set[str] = set()
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "Secret" and name_fragment in doc["metadata"]["name"]:
            keys |= set((doc.get("data") or {}) | (doc.get("stringData") or {}))
    return keys


def test_the_ray_compute_credential_rides_the_infra_secret() -> None:
    """One object, so the dev Secret and the ESO sync cannot disagree about where it lives."""
    keys = _secret_keys(_render(), "infra-credentials")
    assert "rustfs-secret-key" in keys, "this test no longer sees the Secret it is asserting about"
    assert {"ray-compute-access-key", "ray-compute-secret-key"} <= keys, (
        f"the Ray plane's scoped credential is not on infra-credentials; keys present: {sorted(keys)}"
    )


def test_external_secrets_syncs_it_too() -> None:
    """The prod path fills the SAME keys. A key the dev Secret has and ESO does not is a credential
    that exists in dev and is empty in production — the failure this one-object rule prevents."""
    rendered = _render("externalSecrets.enabled=true")
    synced: set[str] = set()
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "ExternalSecret" and "infra-credentials" in doc["metadata"]["name"]:
            synced |= set(((doc["spec"].get("target") or {}).get("template") or {}).get("data") or {})
    assert {"ray-compute-access-key", "ray-compute-secret-key"} <= synced, (
        f"ESO does not sync the Ray credential, so the prod path leaves it empty; it syncs: {sorted(synced)}"
    )


def test_the_ray_pod_takes_BOTH_halves_from_that_secret() -> None:
    """The key and the secret must come from one place. They did not: `S3_KEY` was a literal while
    `S3_SECRET` was a secretKeyRef, so repointing one without the other gave every job
    `SignatureDoesNotMatch` — measured on the live estate."""
    # BOTH flags: `rayservice.yaml` is guarded on `ray.enabled AND singleTenant.enabled`.
    rendered = _render("ray.enabled=true", "singleTenant.enabled=true")
    found: dict[str, str] = {}
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "RayService":
            continue
        # A RayService nests its pod spec under `rayClusterConfig.headGroupSpec`, NOT `spec.template` —
        # reading it as a Deployment finds nothing and the assertion passes over an empty set.
        head = ((doc.get("spec") or {}).get("rayClusterConfig") or {}).get("headGroupSpec") or {}
        for container in ((head.get("template") or {}).get("spec") or {}).get("containers") or []:
            for entry in container.get("env") or []:
                if entry.get("name") in {"S3_KEY", "S3_SECRET"}:
                    found[entry["name"]] = "secretKeyRef" if entry.get("valueFrom") else "literal"
    assert found, "no RayService head spec rendered — this test can no longer see what it asserts about"
    assert found.get("S3_SECRET") == "secretKeyRef", "the secret must never be a literal: it would land in `helm get manifest`"
    assert found.get("S3_KEY") == "secretKeyRef", (
        f"S3_KEY is {found.get('S3_KEY') or 'absent'} — it must come from the same object as the secret, "
        "or the pair has two owners and changing one gives every job SignatureDoesNotMatch"
    )
