"""The chart WRITES the authorization model, so the store is not a third copy nobody checks.

[[LH-174]]. `model.fga` is the source of truth for every `can_*` the code derives, and nothing put it
in OpenFGA. Measured live 2026-09-18: the store's newest model defined 26/27/26 relations on
`warehouse`/`namespace`/`table` against 30/29/29 in the repo — so `warehouse#maintainer` existed in
the model the code reasons about and could not be written to the store at all.

THE FAILURE IS NOT SUBTLE AND IT BLOCKS UPGRADES. A tuple naming a relation the store's model lacks is refused
(measured 2026-09-18: `Invalid tuple 'warehouse:lance_catalog#maintainer@user:service-maintenance'. Reason: relation
'warehouse#maintainer' not found`), and an upgrade whose Job writes one waits on it, leaving the release in
`pending-upgrade`, which the chart's own rules say refuses every later upgrade.

THREE COPIES, AND THE GATE ONLY EVER SAW TWO. `make fga-test` diffs `model.json` against `model.fga`.
The store is the third and was checked by nothing, which is exactly how it fell nine relations behind
without a single test going red.

NO FOURTH COPY. The model already ships INSIDE the service image — `lakehouse/governed/auth/`
carries `model.fga`, `model.fga.yaml` and `model.json` in site-packages — so the hook reads it from
there. Rendering it into a ConfigMap would have created the fourth copy this row is about.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.chart.chart_render import render


REPO = Path(__file__).resolve().parents[2]


def _render(*extra: str) -> list[dict]:
    return list(render("--set-string", "auth.oidc.issuer=http://localhost:8080/dex", "--set", "image.localImages=true", *extra))


def _jobs(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Job"}


@pytest.mark.parametrize("phase", ["install", "upgrade"])
def test_the_store_identities_are_provisioned_before_the_pods_that_use_them(phase: str) -> None:
    """The catalog cannot start until `object-store-provisioning` has written the policy its token is granted. On an
    INSTALL the provisioning Job is part of the manifest, never a hook: Helm runs post-install hooks only once every
    resource passes `--wait`, so a post-install provisioning left the catalog unable to pass it and a fresh
    `helm install --wait` deadlocked ([[XC-152]]). On an UPGRADE it is a pre-upgrade hook, so a new identity's policy
    exists before pods that use it roll ([[XC-133]], CI run 38217921913)."""
    jobs = _jobs(_render(*(["--is-upgrade"] if phase == "upgrade" else [])))
    [provisioning] = [j for name, j in jobs.items() if "-object-store-provisioning" in name and "-object-store-provisioning-prune" not in name]
    hook = (provisioning["metadata"].get("annotations") or {}).get("helm.sh/hook")

    if phase == "install":
        assert hook is None, f"on install the provisioning Job is a {hook!r} hook; the catalog waits on it, so --wait deadlocks"
    else:
        assert hook is not None and "pre-upgrade" in hook, hook
