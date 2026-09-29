"""No workload gains a NEW secret delivered through the environment.

[[LH-160]]. The estate's hard rule (owner, verbatim): *"Never secret through envs. Either from ESO,
secret store dapr and STS for zero trust."* A `secretKeyRef` is a Kubernetes Secret injected as an
environment variable — it is the banned path, and ESO syncing the VALUE from OpenBao does not change
that: it fixes where the secret comes FROM, not how it is DELIVERED.

WHY A RATCHET AND NOT A BAN. There are 30 of these in the render (measured 2026-09-17). A test demanding
zero would be red from the moment it lands and would be skipped or deleted within a week, which is how a
rule with no gate becomes a rule with no effect. A ratchet fails on the THIRTY-FIRST while the rest
migrate, so the number can only fall.

THE RATCHET ALONE SITS STILL AND STAYS GREEN. A budget nobody is obliged to spend down is a budget; what
moves the number is a RULE for the subset with nothing left to build, and this file does not state one.

WHY IT DID NOT EXIST AND WHY THAT MATTERS. Several tests already pin SPECIFIC `secretKeyRef` entries as
CORRECT — the Ray pod's `S3_SECRET`, the app token — each guarding its own plane. Nothing counted them
estate-wide, and that is precisely the regime that let 39 accumulate while every local test stayed
green: the same split this estate already paid for once, when two planes each pinned their own half of
the Jobs-API secret defect and it was fixed twice.

THE BASELINE IS CLASSIFIED BY SANCTIONED PATH, because the fix differs per workload and a bare count
would not say which. A pod WITH a Dapr sidecar should read from the secret store; one WITHOUT should
take an ESO-managed secret as a MOUNTED FILE; anything reaching object storage should hold a vended STS
session instead of a static key at all.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from tests.unit.test_invariants import _helm_template


#: The rendered count on 2026-09-20, measured not guessed. **This number may only go DOWN.**
#: 30 -> 23 when the seven zones' `LINEAGE_SERVICE_TOKEN` became a MOUNTED FILE ([[XC-001]]): the
#: value left the environment, and `readSecretFile` re-reads it per request so an ESO rotation reaches
#: a running pod without the watcher that row's other option would have needed.
#: Lowering it is the point; raising it means a workload took the banned path and the rule lost ground.
SECRET_ENV_BASELINE = 23


def _secret_env_entries() -> list[tuple[str, str, bool]]:
    """Every `secretKeyRef` env entry in the rendered chart, as (workload, var, has_sidecar).

    Read off the RENDER rather than the live cluster so the gate runs without one, and so a change is
    caught in review rather than after it is deployed.
    """
    raw = _helm_template("dapr.enabled=true", "medallion.enabled=true")
    found: list[tuple[str, str, bool]] = []
    for doc in yaml.safe_load_all(raw):
        if not doc or doc.get("kind") not in ("Deployment", "StatefulSet", "Job", "CronJob"):
            continue
        template = doc["spec"].get("template") or doc["spec"].get("jobTemplate", {}).get("spec", {}).get("template")
        if not template:
            continue
        annotations: dict[str, Any] = (template.get("metadata") or {}).get("annotations") or {}
        has_sidecar = annotations.get("dapr.io/enabled") == "true"
        for container in (template.get("spec") or {}).get("containers", []) or []:
            for env in container.get("env") or []:
                if "secretKeyRef" in (env.get("valueFrom") or {}):
                    found.append((doc["metadata"]["name"], env["name"], has_sidecar))
    return found


def test_the_baseline_is_not_stale_upward() -> None:
    """A baseline left above the real count silently re-opens the budget it was meant to close.

    Without this, removing ten entries and forgetting to lower the number would leave room for ten new
    ones — the ratchet still green while the rule lost exactly what it had just won.
    """
    entries = _secret_env_entries()

    assert len(entries) == SECRET_ENV_BASELINE, (
        f"the baseline says {SECRET_ENV_BASELINE} but the render has {len(entries)}. "
        "If you removed some, lower the constant in this commit — a stale-high baseline is unused budget."
    )


#: The two shapes this chart MINTS secret material in: `sha256sum | trunc 40` (hex) and
#: `randAlphaNum 40`. Matching the VALUE rather than the variable's name is what makes this precise —
#: `LANCE_DAPR_SECRET_S3_FIELD=catalog-s3-secret-key` and `LINEAGE_SERVICE_TOKEN_FILE=/etc/...` both
#: read as secretish by name and are exactly the sanctioned paths, while `RASK_APP_TOKEN_FROM_STORE`
#: is a flag. Measured 2026-09-24: the value shape finds six entries and no false positive.
_MINTED_MATERIAL = re.compile(r"^(?:[0-9a-f]{40}|[A-Za-z0-9]{40})$")

#: Entries carrying that material as a LITERAL env value, measured 2026-09-24. All six are
#: `minio-scoped-users`' `mc admin user add` arguments.
#:
#: WORSE THAN A `secretKeyRef`, WHICH IS WHY IT NEEDS ITS OWN NUMBER. The ratchet above counts the
#: banned DELIVERY path; this counts material that never reaches a Secret at all — it is in the
#: rendered manifest, so it is in the Helm release Secret, in `helm get manifest`, and in any GitOps
#: diff. The estate-wide ratchet could not see it: its whole detector is
#: `"secretKeyRef" in env["valueFrom"]`, so it sat green at 23 == 23 while these six rendered beside
#: the one `secretKeyRef` it counted.
#:
#: SIX AND NOT ZERO because the Job that carries them is the estate's only consumer of MinIO's ADMIN
#: API (`policy create`, `user add`), which has no S3 equivalent and which RustFS does not implement —
#: so its fate turns on the object-store ruling ([[XC-075]]) rather than on a rewrite here. A ratchet
#: keeps the number from growing while that is decided; zero is the destination.
#:
#: LOWERING THIS BREAKS A SIBLING GATE ON PURPOSE. `test_one_secret_string_reaches_every_site` asserts
#: the provisioning Job carries each plane's secret as a literal `value: "<secret>"` — it is checking
#: that the Job MINTS what the pods PRESENT, and a literal is merely how it reads that today. Whoever
#: removes these six must replace that hop with a comparison against whatever the Job then reads the
#: secret from, in the same commit. Deleting it instead would leave the pair unchecked at the one hop
#: that creates it.
PLAINTEXT_SECRET_BASELINE = 6


def _plaintext_secret_entries(rendered: str | None = None) -> list[tuple[str, str]]:
    """Every env entry whose literal VALUE is minted secret material, as (workload, var)."""
    raw = rendered if rendered is not None else _helm_template("dapr.enabled=true", "medallion.enabled=true")
    found: list[tuple[str, str]] = []
    for doc in yaml.safe_load_all(raw):
        if not doc or doc.get("kind") not in ("Deployment", "StatefulSet", "Job", "CronJob"):
            continue
        template = doc["spec"].get("template") or doc["spec"].get("jobTemplate", {}).get("spec", {}).get("template")
        if not template:
            continue
        spec = template.get("spec") or {}
        for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
            for env in container.get("env") or []:
                value = env.get("value")
                if isinstance(value, str) and _MINTED_MATERIAL.match(value):
                    found.append((doc["metadata"]["name"], env["name"]))
    return found


def test_the_plaintext_baseline_is_not_stale_upward() -> None:
    """A baseline left above the truth is a budget nobody spends down."""
    entries = _plaintext_secret_entries()
    assert len(entries) == PLAINTEXT_SECRET_BASELINE, (
        f"{len(entries)} plaintext secret entries, baseline {PLAINTEXT_SECRET_BASELINE} — if you REMOVED "
        f"one, lower the baseline in the same commit. Found: {sorted(set(entries))}"
    )
