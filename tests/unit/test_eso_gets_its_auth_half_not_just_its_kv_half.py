"""Turning ESO on must provision the auth backend it authenticates with, not only the KV it reads.

the lakehouse register, row H9 (drained 2026-09-10; in git history), under the owner's 2026-09-08 ruling that zero trust is the goal and
that a secret reaches a workload by ESO, the Dapr secret store, or STS — never through env. ESO is the
destination for the largest class in that survey: the ten `APP_API_TOKEN` refs (whose bootstrap
ordering rules out the Dapr store, since the token is what would authenticate the fetch) plus the
sixteen sidecar-less zone and Ray secrets.

MEASURED 2026-09-08, AND THE HALVES WERE SPLIT. The operator was installed and healthy — three pods,
all 1/1 — with **zero** SecretStores, ExternalSecrets or ClusterSecretStores anywhere in the cluster,
because `externalSecrets.enabled` defaults false. That much is a deliberate default. What was NOT
deliberate is that flipping it could not have worked: the rendered `SecretStore` authenticates with
`auth.kubernetes` at a mount path and a role, and **nothing in this repo enabled that backend or
created that role** — grepped across `chart/templates/*.yaml` and `scripts/*.sh`. The seed Job wrote
`secret/lance` and stopped there.

So the estate had the KV half and not the auth half, and the failure mode is the expensive kind: every
ExternalSecret sits in `SecretSyncedError`, which reads as a broken deploy rather than a missing
prerequisite. This gate is why that cannot happen again — the two halves render together or the test
fails.

THE POLICY IS SCOPED TO ONE PATH, and that is asserted here rather than left to review. ESO is a
controller holding STANDING access, so zero trust applies to the thing fetching the secrets too: read
on exactly `<kvMount>/data/<secretPath>`, no list, no wildcard, no write.
"""

from __future__ import annotations

import re

from tests.unit.test_invariants import _helm_template


def _seed_script() -> str:
    """The OpenBao seed Job's shell body, with ESO switched on."""
    rendered = _helm_template("externalSecrets.enabled=true", "openbao.enabled=true")
    assert "kind: SecretStore" in rendered, "ESO is enabled but no SecretStore renders — the gate would pass vacuously"
    return rendered


def test_the_auth_backend_is_enabled_when_eso_is() -> None:
    """The headline: the SecretStore authenticates with kubernetes auth, so something must enable it."""
    assert "bao auth enable" in _seed_script(), (
        "ESO renders a SecretStore using kubernetes auth and nothing enables that backend — every "
        "ExternalSecret would sit in SecretSyncedError, which looks like a broken deploy rather than a "
        "missing prerequisite"
    )


def test_the_role_is_bound_to_the_operators_service_account() -> None:
    """An unbound role is assumable by any pod in the cluster, which is the opposite of the point."""
    script = _seed_script()

    assert "bound_service_account_names=" in script, "the Vault role is not bound to a ServiceAccount — any pod could assume it"
    assert "bound_service_account_namespaces=" in script, "the Vault role is not bound to a namespace"


def test_the_policy_grants_read_on_exactly_one_path() -> None:
    """ESO holds STANDING access, so its own grant is scoped: read on the one KV path, nothing else."""
    script = _seed_script()
    caps = re.findall(r"capabilities\s*=\s*\[([^\]]*)\]", script)

    assert caps, "the ESO policy grants no capabilities at all — it would authenticate and read nothing"
    granted = {c.strip().strip('"') for cap in caps for c in cap.split(",") if c.strip()}
    assert granted == {"read"}, f"the ESO policy grants more than read: {sorted(granted)}"
    assert "*" not in script.split('path "')[1].split('"')[0], "the ESO policy path is a wildcard rather than the one bundle it needs"


def test_nothing_is_provisioned_when_eso_is_off() -> None:
    """The default stays a no-op: a chart that enabled a Vault auth backend nobody asked for would be
    changing the estate's security posture as a side effect of installing it."""
    assert "bao auth enable" not in _helm_template("openbao.enabled=true"), "the kubernetes auth backend is provisioned without ESO being asked for"
