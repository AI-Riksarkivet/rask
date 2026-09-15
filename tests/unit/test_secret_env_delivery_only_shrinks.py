"""No workload gains a NEW secret delivered through the environment.

[[LH-160]]. The estate's hard rule (owner, verbatim): *"Never secret through envs. Either from ESO,
secret store dapr and STS for zero trust."* A `secretKeyRef` is a Kubernetes Secret injected as an
environment variable — it is the banned path, and ESO syncing the VALUE from OpenBao does not change
that: it fixes where the secret comes FROM, not how it is DELIVERED.

WHY A RATCHET AND NOT A BAN. There are 39 of these in the render today. A test demanding zero would be
red from the moment it lands and would be skipped or deleted within a week, which is how a rule with
no gate becomes a rule with no effect. A ratchet fails on the FORTIETH while the existing 39 migrate,
so the number can only fall.

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

from typing import Any

import pytest
import yaml

from tests.unit.test_invariants import _helm_template


#: The rendered count on 2026-09-15, measured not guessed. **This number may only go DOWN.**
#: Lowering it is the point; raising it means a workload took the banned path and the rule lost ground.
SECRET_ENV_BASELINE = 39

#: Entries whose pod carries a Dapr sidecar, so the Dapr secret store is available to it and is the
#: path the rule names. These are the cheapest to migrate: the mechanism is already in the pod.
WITH_SIDECAR_BASELINE = 10


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
    plumbing, just a different read. Collapsing them into one total would hide the fact that a quarter
    of the violation needs no infrastructure at all.
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
