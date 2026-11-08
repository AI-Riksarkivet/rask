"""A policy the store refuses fails the provisioning hook and leaves the stored one; a pruning run removes what it does not declare.

`chart/templates/object-store-provisioning.yaml` runs `scripts/rustfs_admin.py` on every install and upgrade, writing
each identity's canned policy, the one the store grants a ServiceAccount's token by its subject (ADR 0170). A refusal the
hook let through would report a green upgrade while the store kept the old document, so the hook must exit non-zero on
the first refusal. The store holds its half, that a refused document leaves the old one in place: RustFS checks a
document before it writes it (`rustfs@1.0.3:rustfs/src/admin/handlers/policies.rs:216-226`), so that assert goes red only
on a store that changed, the bump this lane re-runs for.

The refused document is one the script's own check passes (valid JSON, `Version` 2012-10-17) and RustFS refuses: an S3
statement that names no Resource (`rustfs@1.0.3:crates/policy/src/policy/statement.rs:334-342`), which the admin API
answers 400 InvalidRequest. A template edit that drops a policy's Resource line renders exactly this.

[[XC-083]], [[XC-084]] The hook's runs outside the pre-upgrade one pass `--prune`, so the store's identities are what git
declares: no workload signs as a user, so every regular user but the root goes (an earlier release's static
`rask-maintenance`), and so does a rask policy the run no longer creates, while a policy rask does not own is left.
Removing a user ends every credential it signs, so the assert is the store refusing the retired key.

Run by `dagger call rustfs-provisioning` (`make rustfs-provisioning`), which binds a throwaway RustFS to the exec and
names it in the `RASK_RUSTFS_PROVISIONING_*` variables; nothing else sets them, so no other run touches a store's IAM.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import uuid

import pytest
from botocore.exceptions import ClientError
from throwaway_rustfs import SCRIPT, ThrowawayStore, admin, s3_client


pytestmark = pytest.mark.rustfs_provisioning

BUCKET = "refused-policy"


def _policy(statement: dict[str, object]) -> dict[str, object]:
    return {"Version": "2012-10-17", "Statement": [statement]}


#: A grant the store takes.
FINDS_THE_BUCKET = _policy({"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{BUCKET}"]})
#: The same grant with its Resource line dropped.
NAMES_NO_RESOURCE = _policy({"Effect": "Allow", "Action": ["s3:GetBucketLocation"]})


def _provision(store: ThrowawayStore, *, policies: dict[str, dict[str, object]], prune: bool = False) -> subprocess.CompletedProcess[str]:
    """One run of the hook's provisioning step, with the arguments the hook passes it: each policy inline."""
    argv = [
        sys.executable, str(SCRIPT),
        "--endpoint", store.endpoint,
        "--root-access-key", store.root_access_key,
        "--root-secret-file", str(store.root_secret_file),
        "--bucket", BUCKET,
    ]  # fmt: skip
    for name, document in policies.items():
        argv += ["--policy", f"{name}={json.dumps(document)}"]
    if prune:
        argv.append("--prune")
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)


def test_a_policy_the_store_refuses_fails_the_run_and_leaves_the_stored_one(rustfs_store: ThrowawayStore) -> None:
    name = f"system:serviceaccount:refusal:sa-{uuid.uuid4().hex[:8]}"
    provisioned = _provision(rustfs_store, policies={name: FINDS_THE_BUCKET})
    assert provisioned.returncode == 0, provisioned.stderr

    refused = _provision(rustfs_store, policies={name: NAMES_NO_RESOURCE})

    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert re.search(
        rf"^!! rustfs_admin: add-canned-policy \{{'name': '{re.escape(name)}'\}} refused: HTTP 400 .*InvalidRequest", refused.stderr, re.MULTILINE
    ), refused.stderr
    assert json.dumps(admin(rustfs_store).get("list-canned-policies")[name]).count(f"arn:aws:s3:::{BUCKET}") == 1, (
        "the refused document replaced the stored one"
    )


def test_a_pruning_run_removes_every_user_and_every_rask_policy_it_does_not_create(rustfs_store: ThrowawayStore) -> None:
    suffix = uuid.uuid4().hex[:8]
    held, dropped, foreign = f"system:serviceaccount:prune:held-{suffix}", f"rask-prune-dropped-{suffix}", f"ops-prune-{suffix}"
    made = _provision(rustfs_store, policies={held: FINDS_THE_BUCKET, dropped: FINDS_THE_BUCKET, foreign: FINDS_THE_BUCKET})
    assert made.returncode == 0, made.stderr
    retired, retired_secret = f"prune-retired-{suffix}", secrets.token_hex(16)
    store_admin = admin(rustfs_store)
    store_admin.put("add-user", {"accessKey": retired}, json.dumps({"secretKey": retired_secret, "status": "enabled"}).encode())
    store_admin.put("set-user-or-group-policy", {"policyName": foreign, "userOrGroup": retired, "isGroup": "false"}, b"")

    pruned = _provision(rustfs_store, policies={held: FINDS_THE_BUCKET}, prune=True)

    assert pruned.returncode == 0, pruned.stdout + pruned.stderr
    with pytest.raises(ClientError) as gone:
        s3_client(rustfs_store, retired, retired_secret).list_buckets()
    assert gone.value.response["Error"]["Code"] == "InvalidAccessKeyId"
    policies = set(store_admin.get("list-canned-policies"))
    assert (held in policies, dropped in policies, foreign in policies) == (True, False, True), sorted(policies)
