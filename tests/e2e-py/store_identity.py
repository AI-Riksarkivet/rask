"""The live suites' own store identity, as a client outside the catalog's doors reaches the store ([[XC-081]]).

The runner mints a TokenRequest for `rask-e2e-suites` (`Credential.STORE_TOKEN`), and the store's STS exchanges it for a
session under the policy `deploy/values-e2e-suites.yaml` declares: every bucket's listing, the two outboxes, and writes
on the suites' scratch prefixes only. So no suite holds the store's root pair, and the store refuses what that policy does
not grant.
"""

from __future__ import annotations

from credentials import Credential, credential

from lakehouse.storage import sts_client
from lakehouse.storage_options import StorageOptions


#: The role the token is exchanged for: AWS assumes it, a store that grants the policy the token's subject names (RustFS)
#: ignores it, and boto3 requires one.
_ROLE = "arn:aws:iam::000000000000:role/rask-e2e-suites"


def store_options(endpoint: str) -> StorageOptions:
    """An hour's session of the suites' own identity at the store ``endpoint``, as Lance, pyarrow and boto3 read it.

    Exchanged per call: one session for a whole run would outlive its hour on a long one and refuse the later reads.
    """
    token = credential(Credential.STORE_TOKEN)
    assert endpoint and token, "set the store's endpoint and the run's store-token credential: this reads or writes the store itself"
    granted = sts_client(region="us-east-1", endpoint=endpoint).assume_role_with_web_identity(
        RoleArn=_ROLE, RoleSessionName="rask-e2e-suites", WebIdentityToken=token, DurationSeconds=3600
    )["Credentials"]
    return StorageOptions.from_flat(
        {
            "endpoint": endpoint,
            "access_key_id": granted["AccessKeyId"],
            "secret_access_key": granted["SecretAccessKey"],
            "session_token": granted["SessionToken"],
            "region": "us-east-1",
            "allow_http": str(endpoint.startswith("http://")).lower(),
            "virtual_hosted_style_request": "false",
        }
    )
