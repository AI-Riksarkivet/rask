"""The maintenance plane can be given a credential that is not the RustFS tenant root.

`MAINTENANCE_S3_ACCESS_KEY_ID` renders from `.Values.rustfs.accessKey` — `rustfsadmin`, the same pair
the Tenant's credsSecret uses. So the service that compacts every dataset in every bucket does it with
a key that also reaches `_projects/`, `_protection/` and `_policies/`: the records that decide what
maintenance itself is permitted to do. Anything able to run code in that pod can rewrite its own
governing policy.

Vending does not close this on its own, and that was measured rather than assumed. `warehouse.writer`
(`model.fga:97`) carries no `writer from parent` — unlike `namespace.writer` (:258) and `table.writer`
(:330), which both cascade — so a grant covers ONE warehouse, and `can_write_data` for the maintenance
subject on a table under `warehouse:acme-bucket` (one of ~130) returns `allowed:false` with the grant
in place. Every vend outside the default warehouse therefore degrades, and what it degrades TO is the
root key. The fallback itself has to stop being root.

The shape is the one the Ray plane already uses and that was measured-enforced on RustFS 2026-08-30:
a prefix-conditioned user, list/get/put/delete on the data prefixes, nothing on the control plane.

EMPTY MEANS TODAY'S BEHAVIOUR. An estate that has not provisioned the user keeps the root credential
and changes in no way, because a chart that silently repointed a live service at a credential nobody
created would take maintenance down on upgrade.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.test_invariants import _helm_template


def _maintenance_env(rendered: str) -> dict[str, str]:
    """The maintenance Deployment's env, as name -> rendered value."""
    blocks = [b for b in rendered.split("---") if "kind: Deployment" in b and "-maintenance" in b]
    assert blocks, "no maintenance Deployment in the render"
    found: dict[str, str] = {}
    for name, value in re.findall(r"\{\s*name:\s*([A-Z0-9_]+),\s*value:\s*\"?([^\"}\n]*)\"?\s*\}", blocks[0]):
        found[name] = value.strip().strip('"')
    return found


def test_unset_keeps_the_credential_it_has_today() -> None:
    env = _maintenance_env(_helm_template("maintenance.enabled=true"))
    assert env.get("MAINTENANCE_S3_ACCESS_KEY_ID") == "rustfsadmin", (
        "the default changed — an estate that provisioned no scoped user would lose maintenance on upgrade"
    )


def test_a_provisioned_key_replaces_the_tenant_root() -> None:
    rendered = _helm_template(
        "maintenance.enabled=true",
        "rustfs.maintenanceAccessKey=rask-maintenance",
        "rustfs.maintenanceSecretKey=maintenance-secret",
    )
    env = _maintenance_env(rendered)
    assert env.get("MAINTENANCE_S3_ACCESS_KEY_ID") == "rask-maintenance", (
        "maintenance still renders the tenant root even with a scoped user provisioned"
    )
    assert "rustfsadmin" not in env.get("MAINTENANCE_S3_ACCESS_KEY_ID", "")


def test_the_scoped_user_is_actually_provisioned_not_just_referenced() -> None:
    """`values.yaml` already carries `rayComputeAccessKey` with the admission that `scripts/` has no
    provisioning step, so the estate's one scoped-user precedent is a knob only a hand-run `mc`
    session can turn and a fresh install comes up on the tenant root. A second knob with the same hole
    would be worse than none: it would read as hardening that an operator cannot actually apply."""
    rendered = _helm_template(
        "maintenance.enabled=true",
        "rustfs.maintenanceAccessKey=rask-maintenance",
        "rustfs.maintenanceSecretKey=maintenance-secret",
    )
    assert "mc admin user add" in rendered, "no Job creates the user the Deployment now points at"
    assert "mc admin policy" in rendered, "the user is created with no policy, i.e. with whatever RustFS defaults to"


def test_the_policy_denies_the_records_that_govern_maintenance() -> None:
    """The whole point. A compaction credential that can rewrite `_protection/` or `_policies/` can
    turn off the guard that stops it destroying a shallow clone's source, or re-pace itself."""
    rendered = _helm_template(
        "maintenance.enabled=true",
        "rustfs.maintenanceAccessKey=rask-maintenance",
        "rustfs.maintenanceSecretKey=maintenance-secret",
    )
    policy = rendered[rendered.index("mc admin policy") - 4000 : rendered.index("mc admin policy") + 4000]
    for guarded in ("_projects/", "_protection/", "_policies/"):
        assert guarded in policy, f"the policy never mentions {guarded}, so nothing stops the compaction credential rewriting it"


@pytest.mark.parametrize("absent", ["rustfs.maintenanceAccessKey", "rustfs.maintenanceSecretKey"])
def test_half_a_pair_provisions_nothing(absent: str) -> None:
    """Both halves or neither. The Ray plane's own note records the measured cost of setting one:
    every job got `SignatureDoesNotMatch`."""
    sets = {
        "rustfs.maintenanceAccessKey": "rask-maintenance",
        "rustfs.maintenanceSecretKey": "maintenance-secret",
    }
    del sets[absent]
    rendered = _helm_template("maintenance.enabled=true", *(f"{k}={v}" for k, v in sets.items()))
    env = _maintenance_env(rendered)
    assert env.get("MAINTENANCE_S3_ACCESS_KEY_ID") == "rustfsadmin", (
        f"only {next(iter(sets))} was set and the chart repointed anyway — that is SignatureDoesNotMatch on every sweep"
    )


def test_the_policy_covers_every_bucket_the_sweep_is_told_to_sweep() -> None:
    """A policy narrower than the swept set is the worse of the two failure directions.

    `MAINTENANCE_S3_BUCKET` + `MAINTENANCE_S3_EXTRA_BUCKETS` is what the service will actually open. If
    the policy omits one of them the sweep gets AccessDenied on a bucket it is configured to maintain,
    and `compact_one` reports that per dataset as an `open:` error — which reads as a broken dataset,
    not as a missing grant, and is acked as SUCCESS by `ack_for` so it is not even retried.
    """
    rendered = _helm_template(
        "maintenance.enabled=true",
        "rustfs.maintenanceAccessKey=rask-maintenance",
        "rustfs.maintenanceSecretKey=maintenance-secret",
        "catalog.multibase.dataBases[0]=s3://extra-base",
    )
    env = _maintenance_env(rendered)
    swept = [env["MAINTENANCE_S3_BUCKET"], *[b for b in env.get("MAINTENANCE_S3_EXTRA_BUCKETS", "").split(",") if b]]
    assert len(swept) > 1, "set an extra bucket, or this gate only ever checks the primary one"

    job = rendered[rendered.index("component: rustfs-scoped-users") :]
    policy = job[: job.index("rask-ray-compute") if "rask-ray-compute" in job else len(job)]
    for bucket in swept:
        assert f"arn:aws:s3:::{bucket}/*" in policy, (
            f"the sweep is configured to maintain {bucket!r} and the policy does not grant it — every dataset there will 403"
        )


def test_the_scoped_secret_has_its_own_field_in_the_store() -> None:
    """Repointing the ACCESS KEY alone gives SignatureDoesNotMatch on every operation.

    On a governed estate the secret half does not come from pod env at all — `MAINTENANCE_SECRETS_FROM_DAPR`
    sends it to the Dapr secret store, and `dapr_secret_s3_field` names WHICH field to read, defaulting
    to `rustfs-secret-key` (the tenant root's). So a scoped access key with that default reads the root's
    secret and signs with a mismatched pair. The field is already configurable; the chart has to use it.
    """
    rendered = _helm_template(
        "maintenance.enabled=true",
        "rustfs.maintenanceAccessKey=rask-maintenance",
        "rustfs.maintenanceSecretKey=maintenance-secret",
    )
    env = _maintenance_env(rendered)
    assert env.get("MAINTENANCE_DAPR_SECRET_S3_FIELD") == "maintenance-s3-secret-key", (
        "the scoped key would be paired with the tenant root's secret — SignatureDoesNotMatch on every sweep"
    )


def test_the_scoped_secret_is_actually_seeded() -> None:
    """A field the service is told to read and nothing writes is a fail-closed boot, not a fallback:
    `fetch_required_secrets` raises rather than degrading."""
    rendered = _helm_template(
        "maintenance.enabled=true",
        "rustfs.maintenanceAccessKey=rask-maintenance",
        "rustfs.maintenanceSecretKey=maintenance-secret",
    )
    assert "maintenance-s3-secret-key=" in rendered, "nothing seeds the field the Deployment now reads"


def test_unscoped_still_reads_the_field_it_always_did() -> None:
    rendered = _helm_template("maintenance.enabled=true")
    env = _maintenance_env(rendered)
    assert env.get("MAINTENANCE_DAPR_SECRET_S3_FIELD", "rustfs-secret-key") == "rustfs-secret-key"
