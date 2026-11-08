"""Provision rask's store identities on RustFS through its native admin API.

The chart's `object-store-provisioning` hook runs this out of rask's own image, once per install or upgrade, as the
ONLY holder of the store's root pair besides the store itself (rask's runtime makes no admin call). In order:

1. wait for the store to serve S3, bounded;
2. create each named bucket (S3 CreateBucket, idempotent);
3. create or replace each canned policy (`PUT /rustfs/admin/v3/add-canned-policy?name=`, body = the policy JSON);
4. with `--prune`, remove every regular user but the root (`GET list-users`, `DELETE remove-user`), then every canned
   policy rask owns that it does not create (`GET list-canned-policies`, `DELETE remove-canned-policy`), so the store's
   identities are what git declares ([[XC-083]], [[XC-084]]).

NO WORKLOAD IS A USER. Each policy is named after a ServiceAccount (`system:serviceaccount:<ns>:<sa>`), and the store
grants it to the token that account projects (ADR 0170), so the run makes no user and holds no secret but the root's.
Each document arrives in the run's own arguments rather than from a mounted file, so a pre-upgrade run writes the
revision it belongs to: the manifest that would update a mounted ConfigMap is applied only after the hook. The
hook prunes only after an upgrade has replaced every pod, because a pod still signing with a session a removed policy
grants would be cut off mid-flight; `list-users` names no session and no service account
(`rustfs@1.0.3:crates/iam/src/manager.rs:1367-1377`), and a policy rask does not own (RustFS's built-ins, an operator's)
is never removed.

THE ROOT SECRET IS READ FROM A FILE, never env or argv, where a pod spec, `ps` or a crash report would carry it. Requests
are SigV4-signed (service `s3`) with botocore and sent with urllib; the bodies are plain JSON, which the `/rustfs/admin`
prefix takes (the admin API's compatibility alias expects encrypted bodies). Measured on rustfs/rustfs 1.0.1 and 1.0.3
for `add-canned-policy` on 2026-10-18 (scripts/rustfs_sts_measure.py), and on 1.0.3 for `list-users` and `remove-user`
(scripts/rustfs_web_identity_measure.py); a refused policy fails the run before it replaces another, and a pruning run
removes what it does not declare (tests/e2e-py/test_a_policy_the_store_refuses_fails_the_provisioning_hook.py).

    python scripts/rustfs_admin.py --endpoint http://rask-rustfs:9000 --root-access-key rustfsadmin \\
        --root-secret-file /store-root/object-store-secret-key --bucket lance-catalog \\
        --policy 'system:serviceaccount:default:rask-sa-catalog={"Version": "2012-10-17", "Statement": [...]}' --prune
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict


if TYPE_CHECKING:
    from collections.abc import Sequence

    from lakehouse.storage import S3Client


#: The admin API's own prefix, which takes plain JSON bodies; its compatibility alias wants encrypted ones.
ADMIN_PREFIX = "/rustfs/admin/v3"
#: The store's default signing region (`rustfs@1.0.3:crates/config/src/constants/app.rs:289-292`).
REGION = "us-east-1"
#: Bounded: a store silent for two minutes is not slow, it is not coming, and a hook that hangs instead of failing
#: wedges the upgrade until its deadline.
ATTEMPTS = 60
DELAY_SECONDS = 2.0
#: Admin calls get a short retry of their own for a 5xx: RustFS answers `iam is not initialized` while its IAM
#: store loads, which can trail the first S3 answer.
ADMIN_ATTEMPTS = 10
#: The canned policies a run owns, by name: rask's own, and the ones named after a ServiceAccount (ADR 0170).
OWNED_POLICY_PREFIXES = ("rask-", "system:serviceaccount:")


class ProvisionError(RuntimeError):
    """The store refused a provisioning call; the hook exits non-zero naming the call."""


class Plan(BaseModel):
    """Everything one hook run provisions, checked before the first call so a typo fails before any write."""

    model_config = ConfigDict(frozen=True)

    endpoint: str
    root_access_key: str
    root_secret_file: Path
    buckets: tuple[str, ...]
    #: Each canned policy's name and its document, as the JSON text the run was handed.
    policies: dict[str, str]
    prune: bool = False

    def check(self) -> dict[str, bytes]:
        """Refuse a plan that names a missing root secret or a malformed policy document.

        Returns each policy as the JSON body the store is sent, so every document is parsed before the first write.
        """
        if not self.root_secret_file.is_file():
            raise ProvisionError(f"{self.root_secret_file} is not a readable file")
        documents: dict[str, bytes] = {}
        for name, text in self.policies.items():
            try:
                document = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProvisionError(f"policy {name} is not a JSON document: {exc}") from exc
            if not isinstance(document, dict) or document.get("Version") != "2012-10-17":
                # RustFS refuses a canned policy without `Version` (rustfs@1.0.3:rustfs/src/admin/handlers/policies.rs:229-231).
                raise ProvisionError(f"policy {name} names no Version 2012-10-17")
            documents[name] = json.dumps(document).encode()
        return documents


def _read_secret(path: Path) -> str:
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise ProvisionError(f"{path} is empty")
    return secret


def _pair(raw: str, flag: str) -> tuple[str, str]:
    name, sep, value = raw.partition("=")
    if not (sep and name and value):
        raise argparse.ArgumentTypeError(f"{flag} takes NAME=VALUE, got {raw!r}")
    return name, value


class AdminClient:
    """SigV4-signed calls to RustFS's admin API with the root pair."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str) -> None:
        from botocore.credentials import Credentials

        self._endpoint = endpoint.rstrip("/")
        self._credentials = Credentials(access_key, secret_key)

    def put(self, operation: str, query: dict[str, str], body: bytes) -> None:
        """PUT one admin operation; raise `ProvisionError` on a refusal, retry a 5xx a bounded number of times."""
        self._call("PUT", operation, query, body)

    def get(self, operation: str) -> dict[str, object]:
        """GET one admin listing, a JSON object keyed by name."""
        listing = json.loads(self._call("GET", operation, {}, b""))
        if not isinstance(listing, dict):
            raise ProvisionError(f"{operation} answered {type(listing).__name__}, not an object keyed by name")
        return listing

    def delete(self, operation: str, query: dict[str, str]) -> None:
        """DELETE one admin object; one already gone (404) is the outcome asked for."""
        self._call("DELETE", operation, query, b"", absent_ok=True)

    def _call(self, method: str, operation: str, query: dict[str, str], body: bytes, *, absent_ok: bool = False) -> bytes:
        from botocore.auth import S3SigV4Auth
        from botocore.awsrequest import AWSRequest

        # No bare `?`: RustFS decodes an empty query string as a malformed one (400 "failed to decode query", measured on 1.0.3).
        url = f"{self._endpoint}{ADMIN_PREFIX}/{operation}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        for attempt in range(1, ADMIN_ATTEMPTS + 1):
            request = AWSRequest(method=method, url=url, data=body, headers={"Content-Type": "application/json"})
            S3SigV4Auth(self._credentials, "s3", REGION).add_auth(request)
            prepared = urllib.request.Request(url, data=body or None, headers=dict(request.headers.items()), method=method)  # noqa: S310 — the endpoint is the chart's own store URL
            try:
                with urllib.request.urlopen(prepared, timeout=30) as response:  # noqa: S310 — as above
                    return response.read()
            except urllib.error.HTTPError as exc:
                if absent_ok and exc.code == 404:
                    return b""
                # The store's error body names the refused argument; it never echoes a request body, so it carries no secret.
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code < 500 or attempt == ADMIN_ATTEMPTS:
                    raise ProvisionError(f"{operation} {query} refused: HTTP {exc.code} {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt == ADMIN_ATTEMPTS:
                    raise ProvisionError(f"{operation} {query} unreachable: {exc.reason}") from exc
            time.sleep(DELAY_SECONDS)
        raise ProvisionError(f"{operation} {query} was not attempted")


def _s3(plan: Plan, secret: str) -> S3Client:
    from lakehouse.storage import s3_client

    return s3_client(plan.endpoint, access_key=plan.root_access_key, secret_key=secret, region=REGION)


def _await_store(client: S3Client) -> None:
    """Wait out a store that is unreachable or not ready yet (RustFS answers 503 until it is); a refusal below 500, such
    as a wrong root pair, is final and fails at once."""
    from botocore.exceptions import ClientError, HTTPClientError
    from botocore.exceptions import ConnectionError as StoreUnreachableError

    for attempt in range(1, ATTEMPTS + 1):
        try:
            client.list_buckets()
            return
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) < 500:
                raise ProvisionError(f"the object store refused the root pair: {exc}") from exc
            failure: Exception = exc
        except (StoreUnreachableError, HTTPClientError) as exc:
            failure = exc
        if attempt == ATTEMPTS:
            raise ProvisionError(f"the object store never answered after {ATTEMPTS * DELAY_SECONDS:.0f}s: {failure}") from failure
        time.sleep(DELAY_SECONDS)


def _ensure_buckets(client: S3Client, buckets: Sequence[str]) -> None:
    from botocore.exceptions import ClientError

    for bucket in dict.fromkeys(buckets):
        try:
            client.create_bucket(Bucket=bucket)
            print(f"rustfs_admin: created bucket {bucket}")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise ProvisionError(f"CreateBucket {bucket} refused: {code}") from exc
            print(f"rustfs_admin: bucket {bucket} already exists")


def prune(admin: AdminClient, plan: Plan) -> None:
    """Remove every regular user but the root, then every policy rask owns that the plan does not create.

    Users first: removing a user ends its binding, and RustFS refuses removing a bound policy
    (`rustfs@1.0.3:crates/iam/src/manager.rs:555-557`).
    """
    for access_key in sorted(set(admin.get("list-users")) - {plan.root_access_key}):
        admin.delete("remove-user", {"accessKey": access_key})
        print(f"rustfs_admin: removed user {access_key}, which no workload signs as")
    for name in sorted(set(admin.get("list-canned-policies")) - set(plan.policies)):
        if name.startswith(OWNED_POLICY_PREFIXES):
            admin.delete("remove-canned-policy", {"name": name})
            print(f"rustfs_admin: removed undeclared policy {name}")


def provision(plan: Plan) -> None:
    """Run the plan against the store: buckets, then every policy, then the prune."""
    documents = plan.check()
    root_secret = _read_secret(plan.root_secret_file)
    s3 = _s3(plan, root_secret)
    _await_store(s3)
    _ensure_buckets(s3, plan.buckets)

    admin = AdminClient(plan.endpoint, plan.root_access_key, root_secret)
    for name, body in documents.items():
        admin.put("add-canned-policy", {"name": name}, body)
        print(f"rustfs_admin: policy {name} set")
    if plan.prune:
        prune(admin, plan)


def _plan(argv: Sequence[str]) -> Plan:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", required=True, help="the store's S3 endpoint, e.g. http://rask-rustfs:9000")
    parser.add_argument("--root-access-key", required=True, help="the root access key ID (an identifier, not a secret)")
    parser.add_argument("--root-secret-file", required=True, type=Path, help="file holding the root secret key")
    parser.add_argument("--bucket", action="append", default=[], help="a bucket to create if absent (repeatable)")
    parser.add_argument("--policy", action="append", default=[], type=lambda raw: _pair(raw, "--policy"), help="NAME=JSON: a policy, inline (repeatable)")
    parser.add_argument("--prune", action="store_true", help="remove every user but the root, and every rask policy the run does not create")
    args = parser.parse_args(argv)
    return Plan(
        endpoint=args.endpoint,
        root_access_key=args.root_access_key,
        root_secret_file=args.root_secret_file,
        buckets=tuple(args.bucket),
        policies=dict(args.policy),
        prune=args.prune,
    )


def main(argv: Sequence[str]) -> int:
    try:
        provision(_plan(argv))
    except ProvisionError as exc:
        print(f"!! rustfs_admin: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
