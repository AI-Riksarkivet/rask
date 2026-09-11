"""The maintenance plane can be given a credential that is not the RustFS tenant root.

`MAINTENANCE_S3_ACCESS_KEY_ID` renders from `.Values.minio.accessKey` — `minioadmin`, the same pair
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

THE DEFAULT IS THE PROVISIONED IDENTITY (2026-09-08). It was empty — the tenant root — because a
chart repointing a live service at a credential nobody created would take maintenance down on
upgrade. That hazard was ordering, not preference: `minio-scoped-users` ran `post-upgrade`, so the
user was created AFTER the pods had already rolled onto it. The hook now runs `pre-upgrade` as well,
so the credential exists before anything presents it, and explicitly emptying the key is the
deliberate way back to root.
"""

from __future__ import annotations

import re

from tests.unit.test_invariants import _helm_template
from tests.unit.test_scoped_policies_reach_runtime_minted_warehouses import _policy, allowed


def _maintenance_env(rendered: str) -> dict[str, str]:
    """The maintenance Deployment's env, as name -> rendered value."""
    blocks = [b for b in rendered.split("---") if "kind: Deployment" in b and "-maintenance" in b]
    assert blocks, "no maintenance Deployment in the render"
    found: dict[str, str] = {}
    for name, value in re.findall(r"\{\s*name:\s*([A-Z0-9_]+),\s*value:\s*\"?([^\"}\n]*)\"?\s*\}", blocks[0]):
        found[name] = value.strip().strip('"')
    return found


def test_the_default_IS_the_provisioned_identity() -> None:
    """A default install must not present the tenant root for a user the same release provisions.

    This asserted the opposite, guarding an ordering hazard that was real: `minio-scoped-users` ran
    `post-upgrade`, so naming the key rolled pods onto a credential the object store had not heard of.
    The hook now runs `pre-upgrade`, so the user exists before the roll.
    """
    env = _maintenance_env(_helm_template("maintenance.enabled=true"))
    assert env.get("MAINTENANCE_S3_ACCESS_KEY_ID") == "rask-maintenance", "maintenance presents the tenant root on a default install"


def test_a_provisioned_key_replaces_the_tenant_root() -> None:
    rendered = _helm_template(
        "maintenance.enabled=true",
        "minio.maintenanceAccessKey=rask-maintenance",
        "minio.maintenanceSecretKey=maintenance-secret",
    )
    env = _maintenance_env(rendered)
    assert env.get("MAINTENANCE_S3_ACCESS_KEY_ID") == "rask-maintenance", "maintenance still renders the tenant root even with a scoped user provisioned"
    assert "minioadmin" not in env.get("MAINTENANCE_S3_ACCESS_KEY_ID", "")


def test_the_scoped_user_is_actually_provisioned_not_just_referenced() -> None:
    """`values.yaml` already carries `rayComputeAccessKey` with the admission that `scripts/` has no
    provisioning step, so the estate's one scoped-user precedent is a knob only a hand-run `mc`
    session can turn and a fresh install comes up on the tenant root. A second knob with the same hole
    would be worse than none: it would read as hardening that an operator cannot actually apply."""
    rendered = _helm_template(
        "maintenance.enabled=true",
        "minio.maintenanceAccessKey=rask-maintenance",
        "minio.maintenanceSecretKey=maintenance-secret",
    )
    assert "mc admin user add" in rendered, "no Job creates the user the Deployment now points at"
    assert "mc admin policy" in rendered, "the user is created with no policy, i.e. with whatever RustFS defaults to"


def test_the_policy_denies_the_records_that_govern_maintenance() -> None:
    """The whole point. A compaction credential that can rewrite `_protection/` or `_policies/` can
    turn off the guard that stops it destroying a shallow clone's source, or re-pace itself."""
    rendered = _helm_template(
        "maintenance.enabled=true",
        "minio.maintenanceAccessKey=rask-maintenance",
        "minio.maintenanceSecretKey=maintenance-secret",
    )
    # The scoped-users Job's OWN document, not a +/-4000-character window around a marker: the window
    # passed only while the policy happened to sit inside it, so a render that moved anything turned a
    # real assertion into a search of unrelated YAML.
    policy = next(doc for doc in rendered.split("\n---\n") if "component: minio-scoped-users" in doc)
    for guarded in ("_projects/", "_protection/", "_policies/"):
        assert guarded in policy, f"the policy never mentions {guarded}, so nothing stops the compaction credential rewriting it"


def test_a_secret_alone_attaches_to_the_identity_the_chart_already_names() -> None:
    """A secret with no access key used to identify nobody, so it had to leave the plane on root. Now
    the chart always names one, so an operator supplying only a secret is supplying the secret FOR
    that identity — which is the whole point of `lance.scopedStorageSecret` being derivable: a values
    file can declare an identity, or a secret, or both, and never a mismatched pair."""
    env = _maintenance_env(_helm_template("maintenance.enabled=true", "minio.maintenanceSecretKey=maintenance-secret"))
    assert env.get("MAINTENANCE_S3_ACCESS_KEY_ID") == "rask-maintenance", "a supplied secret detached the plane from its provisioned identity"
    assert env.get("MAINTENANCE_DAPR_SECRET_S3_FIELD") == "maintenance-s3-secret-key", "the identity and its secret field came apart"


def test_the_policy_covers_every_bucket_the_sweep_is_told_to_sweep() -> None:
    """A policy narrower than the swept set is the worse of the two failure directions.

    `MAINTENANCE_S3_BUCKET` + `MAINTENANCE_S3_EXTRA_BUCKETS` is what the service will actually open. If
    the policy omits one of them the sweep gets AccessDenied on a bucket it is configured to maintain,
    and `compact_one` reports that per dataset as an `open:` error — which reads as a broken dataset,
    not as a missing grant, and is acked as SUCCESS by `ack_for` so it is not even retried.
    """
    rendered = _helm_template(
        "maintenance.enabled=true",
        "minio.maintenanceAccessKey=rask-maintenance",
        "minio.maintenanceSecretKey=maintenance-secret",
        "catalog.multibase.dataBases[0]=s3://extra-base",
    )
    env = _maintenance_env(rendered)
    swept = [env["MAINTENANCE_S3_BUCKET"], *[b for b in env.get("MAINTENANCE_S3_EXTRA_BUCKETS", "").split(",") if b]]
    assert len(swept) > 1, "set an extra bucket, or this gate only ever checks the primary one"

    # EVALUATED, not grepped. This asserted `arn:aws:s3:::<bucket>/*` appeared verbatim, which tied it
    # to one policy shape and — worse — could only ever check the CONFIGURED buckets, while
    # `sweep.py::_buckets_to_sweep` extends that list from the warehouse registry at runtime. The
    # registry-discovered half is gated by `test_scoped_policies_reach_runtime_minted_warehouses`.
    policy = _policy(rendered, "maintenance")
    for bucket in swept:
        assert allowed(policy, action="s3:PutObject", bucket=bucket, key="medallion/silver/data/0.lance"), (
            f"the sweep is configured to maintain {bucket!r} and the policy does not grant it — every dataset there will 403"
        )


def test_the_scoped_secret_has_its_own_field_in_the_store() -> None:
    """Repointing the ACCESS KEY alone gives SignatureDoesNotMatch on every operation.

    On a governed estate the secret half does not come from pod env at all — `MAINTENANCE_SECRETS_FROM_DAPR`
    sends it to the Dapr secret store, and `dapr_secret_s3_field` names WHICH field to read, defaulting
    to `minio-secret-key` (the tenant root's). So a scoped access key with that default reads the root's
    secret and signs with a mismatched pair. The field is already configurable; the chart has to use it.
    """
    rendered = _helm_template(
        "maintenance.enabled=true",
        "minio.maintenanceAccessKey=rask-maintenance",
        "minio.maintenanceSecretKey=maintenance-secret",
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
        "minio.maintenanceAccessKey=rask-maintenance",
        "minio.maintenanceSecretKey=maintenance-secret",
    )
    assert "maintenance-s3-secret-key=" in rendered, "nothing seeds the field the Deployment now reads"


def test_the_secret_field_follows_the_identity_by_default() -> None:
    """THE PAIR, on a default install. A scoped access key left on the tenant root's secret field is
    signed against the wrong secret and fails every S3 call with SignatureDoesNotMatch."""
    env = _maintenance_env(_helm_template("maintenance.enabled=true"))
    assert env.get("MAINTENANCE_DAPR_SECRET_S3_FIELD") == "maintenance-s3-secret-key", "the scoped key is paired with the tenant root's secret field"
