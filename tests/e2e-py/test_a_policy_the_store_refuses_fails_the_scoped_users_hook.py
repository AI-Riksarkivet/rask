"""A policy the store refuses fails the scoped-users hook and binds no one; a pruning run removes what it does not declare.

`chart/templates/object-store-scoped-users.yaml` runs `scripts/rustfs_admin.py` on every install and upgrade: each
canned policy first, then each user and its binding. A refusal the hook let through would report a green upgrade while
the store kept the old document, and a user bound to a policy the store never took is refused STS AssumeRole, which
every vend goes through (`rustfs@1.0.3:crates/iam/src/sys.rs:1511-1525`). So the hook must exit non-zero on the first
refusal, before it makes or binds anyone. The last assert holds the store to its half, that a refused document leaves
the old one and every binding in place: RustFS checks a document before it writes it and refuses removing a bound policy
(`rustfs@1.0.3:crates/iam/src/manager.rs:555-557`), so that assert goes red only on a store that changed, the bump this
lane re-runs for.

The refused document is one the script's own check passes (valid JSON, `Version` 2012-10-17) and RustFS refuses: an S3
statement that names no Resource (`rustfs@1.0.3:crates/policy/src/policy/statement.rs:334-342`), which the admin API
answers 400 InvalidRequest (`rustfs@1.0.3:rustfs/src/admin/handlers/policies.rs:216-226`). A template edit that drops a
policy's Resource line renders exactly this.

[[XC-083]] The hook's runs outside the pre-upgrade one pass `--prune`, so the store's identities are what git declares: a
user an earlier release made and this one does not declare (the catalog's retired `rask-catalog`) is removed, and so is a
rask policy it no longer creates, while a policy rask does not own is left. Removing a user ends every credential it
signs, so the assert is the store refusing the retired key.

Run by `dagger call rustfs-provisioning` (`make rustfs-provisioning`), which binds a throwaway RustFS to the exec and
names it in the `RASK_RUSTFS_PROVISIONING_*` variables; nothing else sets them, so no other run touches a store's IAM.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict


pytestmark = pytest.mark.rustfs_provisioning

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rustfs_admin.py"
BUCKET = "refused-policy"


class ThrowawayStore(BaseModel):
    """The RustFS the lane binds, and where its root secret is: the script reads it from a file, as the hook does."""

    model_config = ConfigDict(frozen=True)

    endpoint: str
    root_access_key: str
    root_secret_file: Path


class Identity(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_key: str
    secret_key: str


@pytest.fixture
def store() -> ThrowawayStore:
    endpoint = os.environ.get("RASK_RUSTFS_PROVISIONING_ENDPOINT")
    if not endpoint:
        pytest.skip("RASK_RUSTFS_PROVISIONING_ENDPOINT names no throwaway RustFS; `dagger call rustfs-provisioning` binds one")
    return ThrowawayStore(
        endpoint=endpoint,
        root_access_key=os.environ["RASK_RUSTFS_PROVISIONING_ROOT_ACCESS_KEY"],
        root_secret_file=Path(os.environ["RASK_RUSTFS_PROVISIONING_ROOT_SECRET_FILE"]),
    )


def _policy(statement: dict[str, object]) -> dict[str, object]:
    return {"Version": "2012-10-17", "Statement": [statement]}


#: What the bound user may do before the refused run, and must still do after it.
FINDS_THE_BUCKET = _policy({"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{BUCKET}"]})
#: The same grant with its Resource line dropped.
NAMES_NO_RESOURCE = _policy({"Effect": "Allow", "Action": ["s3:GetBucketLocation"]})


def _identity(keys: Path, prefix: str) -> Identity:
    """A user the hook may provision: its secret in `keys/<access key>`, the file the hook's derive step writes."""
    identity = Identity(access_key=f"{prefix}{uuid.uuid4().hex[:8]}", secret_key=secrets.token_hex(16))
    (keys / identity.access_key).write_text(identity.secret_key, encoding="utf-8")
    return identity


def _provision(
    store: ThrowawayStore, keys: Path, *, policies: dict[str, dict[str, object]], users: Sequence[tuple[Identity, str]], prune: bool = False
) -> subprocess.CompletedProcess[str]:
    """One run of the hook's provisioning step, with the arguments the hook passes it: each user bound to its policy."""
    argv = [
        sys.executable, str(SCRIPT),
        "--endpoint", store.endpoint,
        "--root-access-key", store.root_access_key,
        "--root-secret-file", str(store.root_secret_file),
        "--keys-dir", str(keys),
        "--bucket", BUCKET,
    ]  # fmt: skip
    for name, document in policies.items():
        document_file = keys.parent / f"{name}.json"
        document_file.write_text(json.dumps(document), encoding="utf-8")
        argv += ["--policy", f"{name}={document_file}"]
    for user, policy in users:
        argv += ["--user", f"{user.access_key}={policy}"]
    if prune:
        argv.append("--prune")
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)


def _canned_policies(store: ThrowawayStore) -> set[str]:
    """The names of the store's canned policies, read with its root pair through the script's own admin client."""
    spec = importlib.util.spec_from_file_location("rustfs_admin", SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    admin = script.AdminClient(store.endpoint, store.root_access_key, store.root_secret_file.read_text(encoding="utf-8").strip())
    return set(admin.get("list-canned-policies"))


def _client(store: ThrowawayStore, identity: Identity):  # noqa: ANN202 — boto3's client type is generated at runtime
    return boto3.client(
        "s3",
        endpoint_url=store.endpoint,
        aws_access_key_id=identity.access_key,
        aws_secret_access_key=identity.secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )


def test_a_policy_the_store_refuses_fails_the_run_before_it_binds_anyone(store: ThrowawayStore, tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    bound, unbound = _identity(keys, "refusal-bound-"), _identity(keys, "refusal-new-")
    name = f"rask-refusal-{uuid.uuid4().hex[:8]}"
    provisioned = _provision(store, keys, policies={name: FINDS_THE_BUCKET}, users=[(bound, name)])
    assert provisioned.returncode == 0, provisioned.stderr

    refused = _provision(store, keys, policies={name: NAMES_NO_RESOURCE}, users=[(bound, name), (unbound, name)])

    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert re.search(rf"^!! rustfs_admin: add-canned-policy \{{'name': '{name}'\}} refused: HTTP 400 .*InvalidRequest", refused.stderr, re.MULTILINE), (
        refused.stderr
    )
    with pytest.raises(ClientError) as never_made:
        _client(store, unbound).list_buckets()
    assert never_made.value.response["Error"]["Code"] == "InvalidAccessKeyId"
    assert _client(store, bound).get_bucket_location(Bucket=BUCKET)["ResponseMetadata"]["HTTPStatusCode"] == 200


def test_a_pruning_run_removes_every_user_and_rask_policy_it_does_not_declare(store: ThrowawayStore, tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    keys.mkdir()
    kept, retired = _identity(keys, "prune-kept-"), _identity(keys, "prune-retired-")
    suffix = uuid.uuid4().hex[:8]
    held, dropped, foreign = f"rask-prune-held-{suffix}", f"rask-prune-dropped-{suffix}", f"ops-prune-{suffix}"
    made = _provision(
        store, keys, policies={held: FINDS_THE_BUCKET, dropped: FINDS_THE_BUCKET, foreign: FINDS_THE_BUCKET}, users=[(kept, held), (retired, dropped)]
    )
    assert made.returncode == 0, made.stderr

    pruned = _provision(store, keys, policies={held: FINDS_THE_BUCKET}, users=[(kept, held)], prune=True)

    assert pruned.returncode == 0, pruned.stdout + pruned.stderr
    with pytest.raises(ClientError) as gone:
        _client(store, retired).list_buckets()
    assert gone.value.response["Error"]["Code"] == "InvalidAccessKeyId"
    assert _client(store, kept).get_bucket_location(Bucket=BUCKET)["ResponseMetadata"]["HTTPStatusCode"] == 200
    policies = _canned_policies(store)
    assert (held in policies, dropped in policies, foreign in policies) == (True, False, True), sorted(policies)
