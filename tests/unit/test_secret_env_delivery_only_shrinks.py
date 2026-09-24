"""No workload gains a NEW secret delivered through the environment.

[[LH-160]]. The estate's hard rule (owner, verbatim): *"Never secret through envs. Either from ESO,
secret store dapr and STS for zero trust."* A `secretKeyRef` is a Kubernetes Secret injected as an
environment variable — it is the banned path, and ESO syncing the VALUE from OpenBao does not change
that: it fixes where the secret comes FROM, not how it is DELIVERED.

WHY A RATCHET AND NOT A BAN. There are 30 of these in the render (measured 2026-09-17). A test demanding
zero would be red from the moment it lands and would be skipped or deleted within a week, which is how a
rule with no gate becomes a rule with no effect. A ratchet fails on the THIRTY-FIRST while the rest
migrate, so the number can only fall.

THE RATCHET ALONE WOULD SIT STILL AND STAY GREEN, which is why it is not the only thing here. A budget
nobody is obliged to spend down is a budget; `test_a_pod_that_CAN_read_the_store_does_not_take_its_app_token_through_env`
below states the RULE for the subset with nothing left to build, and that is what moves the number.

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

import pytest
import yaml

from tests.unit.test_invariants import _helm_template


#: The rendered count on 2026-09-20, measured not guessed. **This number may only go DOWN.**
#: 30 -> 23 when the seven zones' `LINEAGE_SERVICE_TOKEN` became a MOUNTED FILE ([[XC-001]]): the
#: value left the environment, and `readSecretFile` re-reads it per request so an ESO rotation reaches
#: a running pod without the watcher that row's other option would have needed.
#: Lowering it is the point; raising it means a workload took the banned path and the rule lost ground.
SECRET_ENV_BASELINE = 23

#: Entries whose pod carries a Dapr sidecar, so the Dapr secret store is available to it and is the
#: path the rule names. These are the cheapest to migrate: the mechanism is already in the pod. The one
#: that remains is `compute`, which is NOT in `lance-secrets`' `scopes:` — see `_UNREACHABLE_STORE`.
WITH_SIDECAR_BASELINE = 1


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


def test_the_render_still_produces_workloads() -> None:
    """Without this the ratchet would pass by measuring an empty render."""
    assert _secret_env_entries(), "no secretKeyRef entries found at all — the render or the walk broke, and this gate is checking nothing"


def test_no_new_secret_is_delivered_through_the_environment() -> None:
    """The ratchet. It fails on the FORTIETH, never on the existing thirty-nine.

    If this is red because you ADDED one: the workload's sanctioned path is the Dapr secret store when
    it has a sidecar, an ESO-managed secret mounted as a FILE when it does not, and a vended STS
    session for anything reaching object storage. A scoped static key is not a fix.

    If this is red because you REMOVED one: lower `SECRET_ENV_BASELINE` in the same commit. That is the
    number going the right way and the gate is asking you to record it.
    """
    entries = _secret_env_entries()

    assert len(entries) <= SECRET_ENV_BASELINE, (
        f"{len(entries)} secret env entries, baseline {SECRET_ENV_BASELINE} — a workload took the banned path. New: {sorted({(w, v) for w, v, _ in entries})}"
    )


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


def test_the_sidecar_bearing_entries_are_tracked_separately() -> None:
    """These are the cheapest to migrate, so they are counted apart from the rest.

    A pod with a sidecar already has the Dapr secret store reachable — no new mechanism, no chart
    plumbing, just a different read. Collapsing them into one total would hide the fact that part of the
    violation needs no infrastructure at all.
    """
    with_sidecar = [(w, v) for w, v, car in _secret_env_entries() if car]

    assert len(with_sidecar) == WITH_SIDECAR_BASELINE, (
        f"{len(with_sidecar)} sidecar-bearing secret env entries, baseline {WITH_SIDECAR_BASELINE}: {sorted(with_sidecar)}"
    )


#: The ONE workload that pulls a whole Secret through `envFrom`, and it is not ours to edit.
#: `greptimedb-standalone` is a third-party SUBCHART; the `envFrom` is in its own template, and it
#: pulls `rask-observability-s3` — the object-store credentials for the observability bucket.
#:
#: RECORDED AS A NAMED EXCEPTION RATHER THAN EXEMPTED BY A WILDCARD, so this stays a ban for every
#: template the estate authors: a new `envFrom` anywhere reds here. Tracked as [[LH-161]] — the fix is
#: an upstream change or a values-level override, not an edit we can make in `chart/templates/`.
_ENVFROM_EXCEPTIONS = [("release-name-greptimedb-standalone", "release-name-greptimedb-standalone")]


@pytest.mark.parametrize("banned", ["envFrom"])
def test_no_workload_takes_a_whole_secret_through_envFrom(banned: str) -> None:
    """`envFrom` is named in the rule explicitly, and it is strictly worse than a keyed ref.

    A `secretKeyRef` names one key; `envFrom` pulls EVERY key in the Secret into the process
    environment, so a key added to that Secret for an unrelated consumer silently lands in this
    workload too. Asserted at zero rather than ratcheted because there are none — a rule with no
    existing violations should be a ban, not a budget.
    """
    raw = _helm_template("dapr.enabled=true", "medallion.enabled=true")
    offenders = []
    for doc in yaml.safe_load_all(raw):
        if not doc or doc.get("kind") not in ("Deployment", "StatefulSet", "Job", "CronJob"):
            continue
        template = doc["spec"].get("template") or doc["spec"].get("jobTemplate", {}).get("spec", {}).get("template")
        if not template:
            continue
        for container in (template.get("spec") or {}).get("containers", []) or []:
            for source in container.get(banned) or []:
                if "secretRef" in source:
                    offenders.append((doc["metadata"]["name"], container.get("name")))

    assert sorted(offenders) == sorted(_ENVFROM_EXCEPTIONS), (
        f"the set of workloads pulling a WHOLE secret via {banned} changed.\n"
        f"  found:    {sorted(offenders)}\n  recorded: {sorted(_ENVFROM_EXCEPTIONS)}\n"
        "A new one is the banned path; a removed one means lowering the recorded set in this commit."
    )


def _lance_secrets_scopes() -> list[str]:
    """The app-ids the `lance-secrets` Component admits, read off the render.

    Read from the COMPONENT rather than recomputed from values, because the question this file asks is
    whether a pod can reach the store — and the Component's `scopes:` is what daprd enforces. A
    reimplementation here would answer about the chart's intent instead of about the deployed control.
    """
    for doc in yaml.safe_load_all(_helm_template("dapr.enabled=true", "medallion.enabled=true")):
        if doc and doc.get("kind") == "Component" and doc["metadata"]["name"] == "lance-secrets":
            return sorted(doc.get("scopes") or [])
    return []


def test_the_secret_store_is_scoped_to_someone() -> None:
    """Without this the rule below would pass by measuring an empty scope list."""
    assert _lance_secrets_scopes(), "`lance-secrets` renders with no scopes — the rule below would then be vacuous"


def test_a_pod_that_CAN_read_the_store_does_not_take_its_app_token_through_env() -> None:
    """THE RULE, not the count: where the sanctioned path is already in the pod, it must be the one used.

    `WITH_SIDECAR_BASELINE` above counts these and lets them fall; it does not say they are wrong, so a
    ratchet alone would sit at ten forever and stay green. This asserts the thing the owner's rule
    actually says — *"Never secret through envs. Either from ESO, secret store dapr and STS for zero
    trust"* — for the subset where there is nothing left to build: a pod with a Dapr sidecar whose
    app-id is in `lance-secrets`' `scopes:` can already `GET /v1.0/secrets/lance-secrets/lance`, and
    `expected_app_token()` reads exactly that when `RASK_APP_TOKEN_FROM_STORE` is set.

    SCOPED, AND THAT BOUND IS THE POINT. A pod that is NOT in `scopes:` cannot be fixed by flipping a
    flag — its sidecar answers `ERR_SECRET_STORES_NOT_CONFIGURED` and the app fails closed at boot
    (`assert_app_token_configured`). `compute` is measured in exactly that state, which is why it is
    named here rather than silently passing: adding it to the scope list is a separate edit, and this
    test is where that edit gets recorded when it happens.
    """
    scoped = set(_lance_secrets_scopes())
    offenders = []
    for doc in yaml.safe_load_all(_helm_template("dapr.enabled=true", "medallion.enabled=true")):
        if not doc or doc.get("kind") not in ("Deployment", "StatefulSet"):
            continue
        template = doc["spec"].get("template")
        if not template:
            continue
        annotations: dict[str, Any] = (template.get("metadata") or {}).get("annotations") or {}
        app_id = annotations.get("dapr.io/app-id")
        if annotations.get("dapr.io/enabled") != "true" or app_id not in scoped:
            continue
        for container in (template.get("spec") or {}).get("containers", []) or []:
            for env in container.get("env") or []:
                if env["name"] == "APP_API_TOKEN" and "secretKeyRef" in (env.get("valueFrom") or {}):
                    offenders.append((doc["metadata"]["name"], app_id))

    assert offenders == [], (
        f"{len(offenders)} workloads are scoped to `lance-secrets` and still take APP_API_TOKEN through the environment: {sorted(offenders)}. "
        'Render `RASK_APP_TOKEN_FROM_STORE: "true"` instead — the token stays in OpenBao and what ships is which source to read.'
    )


#: Sidecar-bearing workloads that are NOT scoped to `lance-secrets`, so the flag alone would crash-loop
#: them. Recorded rather than ignored: this is the list of edits the rule still needs, and it shrinks by
#: adding an app-id to the Component's `scopes:` — never by deleting a row from here.
_UNREACHABLE_STORE = [("rask-compute", "compute")]


def test_the_pods_that_CANNOT_reach_the_store_are_named() -> None:
    """A workload the rule cannot yet reach is a recorded gap, not an absence.

    The test above passes for `compute` by construction — it is out of scope, so it is not examined —
    and that is exactly how a violation becomes invisible. This names the population the other test
    skips, so growing it reds here.
    """
    scoped = set(_lance_secrets_scopes())
    unreachable = []
    for doc in yaml.safe_load_all(_helm_template("dapr.enabled=true", "medallion.enabled=true")):
        if not doc or doc.get("kind") not in ("Deployment", "StatefulSet"):
            continue
        template = doc["spec"].get("template")
        if not template:
            continue
        annotations: dict[str, Any] = (template.get("metadata") or {}).get("annotations") or {}
        if annotations.get("dapr.io/enabled") != "true":
            continue
        app_id = annotations.get("dapr.io/app-id")
        names = [env["name"] for container in (template.get("spec") or {}).get("containers", []) or [] for env in container.get("env") or []]
        if "APP_API_TOKEN" in names and app_id not in scoped:
            unreachable.append((doc["metadata"]["name"], app_id))

    assert sorted(unreachable) == sorted(_UNREACHABLE_STORE), (
        f"the set of sidecar pods that cannot reach `lance-secrets` changed.\n  found:    {sorted(unreachable)}\n  recorded: {sorted(_UNREACHABLE_STORE)}\n"
        "A new one needs its app-id in the Component's `scopes:`; a removed one means deleting its row here in the same commit."
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


def test_the_scan_catches_material_planted_in_a_value() -> None:
    """Non-vacuity, proven rather than asserted: a scanner that matched nothing would pass forever.

    This is the failure mode the estate-wide ratchet above actually had — a detector that could not
    express the shape it was meant to catch, staying green at its own baseline.
    """
    planted = (
        "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: planted\n"
        "spec:\n  template:\n    spec:\n      containers:\n        - name: c\n"
        '          env:\n            - { name: SOME_SECRET, value: "' + "a" * 40 + '" }\n'
    )
    assert _plaintext_secret_entries(planted) == [("planted", "SOME_SECRET")]


def test_no_new_secret_material_is_written_into_a_manifest() -> None:
    entries = _plaintext_secret_entries()
    assert len(entries) <= PLAINTEXT_SECRET_BASELINE, (
        f"{len(entries)} env entries carry minted secret material as a literal value, baseline "
        f"{PLAINTEXT_SECRET_BASELINE}. That material is in the rendered manifest, so it is in the Helm "
        f"release Secret and in every GitOps diff — worse than the `secretKeyRef` the ratchet above "
        f"bans. Found: {sorted(set(entries))}"
    )


def test_the_plaintext_baseline_is_not_stale_upward() -> None:
    """A baseline left above the truth is a budget nobody spends down."""
    entries = _plaintext_secret_entries()
    assert len(entries) == PLAINTEXT_SECRET_BASELINE, (
        f"{len(entries)} plaintext secret entries, baseline {PLAINTEXT_SECRET_BASELINE} — if you REMOVED "
        f"one, lower the baseline in the same commit. Found: {sorted(set(entries))}"
    )
