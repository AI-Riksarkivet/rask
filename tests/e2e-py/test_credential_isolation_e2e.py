"""A vended credential reaches its own table at its own tier, and the store refuses it everywhere else (#74).

Taken from the attacker's side: the job identity asks the catalog for a credential on a table of tenant B, where it holds
`writer`, then builds a raw S3 client from it and points it elsewhere. The refusal must come from the STORE, since the
catalog is out of the request path once a direct credential exists; that is why the session policy has to be right
(`catalog.core.vending.build_session_policy`).

Every leg attacks a key the session policy would have to name to allow it, at the table's real location, read off
`describe` and never composed (`lance_docs/ns_catalog/catalog/dir/index.md`, a table's directory is the catalog's to
choose): a key outside the vended prefix is refused whatever the tier allows, so a leg aimed there could not fail.

- Its own table: each tier lists it, and at one data-file key the write tier's put lands while the read tier's is refused.
- A sibling table of the same tenant, in the same bucket: refused at both tiers, which tells a policy scoped to the
  table's prefix from one scoped to the bucket.
- A table of tenant A, where the job holds nothing: list, read and write are each refused, and a read is refused as
  AccessDenied, never NoSuchKey, which would mean the store evaluated the request inside A's bucket.

NOT mockable: the point is the object store's own evaluation of the session policy, which moto's STS does not perform.
Both tenants are this module's own (`scratch_tenant.provisioned`), created per run and removed with their buckets.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import boto3
import pyarrow as pa
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel
from scratch_tenant import CATALOG, DEX, Identity, Tenant, ok, provisioned

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.storage_options import ENDPOINT


S3 = os.environ.get("LANCE_E2E_S3", "")
REFUSED = ("AccessDenied", "AccessDeniedException")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (CATALOG and DEX and S3), reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX and LANCE_E2E_S3 (a deployed estate)"),
]


class Placed(BaseModel):
    """A table and where its bytes are: its bucket and its key prefix there, as `describe` answers them."""

    table: str
    bucket: str
    prefix: str


class Estates(BaseModel):
    """Tenant A's table, and tenant B's two tables in one bucket."""

    a: Placed
    b: Placed
    b_sibling: Placed


def _create(tenant: Tenant, name: str) -> Placed:
    """The tenant's admin creates ``name`` in its bronze namespace; where the catalog put it."""
    table = f"{tenant.bronze}${name}"
    ok(
        tenant.admin.http.post(
            f"{CATALOG}/v1/table/{table}/create",
            data=encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())})),
            headers={"content-type": ARROW_STREAM_MEDIA_TYPE},
            timeout=60,
        )
    )
    tenant.tables.append(table)
    location = str(ok(tenant.admin.http.post(f"{CATALOG}/v1/table/{table}/describe", json={}, timeout=60))["location"])
    bucket, _, prefix = location.removeprefix("s3://").partition("/")
    return Placed(table=table, bucket=bucket, prefix=prefix.rstrip("/"))


@pytest.fixture(scope="module")
def tenant_a() -> Iterator[Tenant]:
    yield from provisioned("iso-a", grants=())


@pytest.fixture(scope="module")
def tenant_b() -> Iterator[Tenant]:
    yield from provisioned("iso-b", grants=("writer",))


@pytest.fixture(scope="module")
def estates(tenant_a: Tenant, tenant_b: Tenant) -> Estates:
    return Estates(a=_create(tenant_a, "t"), b=_create(tenant_b, "t"), b_sibling=_create(tenant_b, "sibling"))


def _vend(identity: Identity, table: str, tier: str) -> dict[str, str]:
    """The ``storage_options`` the catalog vends ``identity`` for ``table`` at ``tier``."""
    body = ok(identity.http.post(f"{CATALOG}/management/v1/table/{table}/credentials", params={"tier": tier}, timeout=30))
    if body.get("mode") != "direct":
        pytest.skip(f"the estate vends {body.get('mode')}: a session policy is attackable only in direct mode")
    options = (body.get("credentials") or {}).get("storage_options")
    assert options, f"the vend carried no storage_options: {body}"
    return options


def _client(creds: dict[str, str]) -> Any:  # noqa: ANN401 — boto3's client is untyped
    """A raw S3 client wearing the vended credential, the catalog out of its path.

    The vended endpoint is the store's in-cluster Service name, which the runner off the cluster cannot resolve, so only
    the HOST is swapped for the address the runner reaches the store at. The key, secret and session token are used as
    vended, and the store evaluates the session policy against bucket and key, not against the address a request came in
    on. The keys are read under the `aws_`-prefixed names the catalog emits for Lance (`tests/unit/test_vending.py`).
    """
    endpoint = creds.get(ENDPOINT) or S3
    vended = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    reachable = urlparse(S3 if "://" in S3 else f"http://{S3}")
    if vended.hostname != reachable.hostname:
        endpoint = f"{vended.scheme}://{reachable.netloc}"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        aws_session_token=creds.get("aws_session_token"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name=creds.get("region") or "us-east-1",
    )


def _refusal(call: Any) -> str:  # noqa: ANN401 — a boto3 call
    """The store's error code for ``call``, or ``"taken"`` when the store answered it."""
    try:
        call()
    except ClientError as error:
        return str(error.response["Error"]["Code"])
    return "taken"


def _a_data_file(placed: Placed) -> str:
    """A new key under the table's `data/`, where the write tier stages a writer's files (`WRITER_PUT_GLOBS`)."""
    return f"{placed.prefix}/data/{uuid.uuid4().hex}.lance"


@pytest.mark.parametrize("tier", ["read", "write"])
def test_a_credential_lists_its_own_table(tenant_b: Tenant, estates: Estates, tier: str) -> None:
    """Without this, a credential refused everywhere would pass every refusal below while breaking the product. The
    trailing slash is load-bearing: the policy gates `s3:ListBucket` on `s3:prefix` matching `<prefix>/*`."""
    s3 = _client(_vend(tenant_b.job, estates.b.table, tier))

    listing = s3.list_objects_v2(Bucket=estates.b.bucket, Prefix=f"{estates.b.prefix}/", MaxKeys=5)

    assert listing.get("KeyCount", 0) > 0, f"a {tier}-tier credential cannot list its own table at {estates.b.prefix!r}"


@pytest.mark.parametrize(("tier", "answer"), [pytest.param("read", "AccessDenied", id="read-is-refused"), pytest.param("write", "taken", id="write-lands")])
def test_a_put_into_the_tables_own_data_is_answered_by_its_tier(tenant_b: Tenant, estates: Estates, tier: str, answer: str) -> None:
    """The tier split is a boundary the store holds: at one key under the table's own `data/`, the write tier's put lands
    and the read tier's is refused, so the read leg fails if the read vend ever grants `PutObject` on its prefix."""
    s3 = _client(_vend(tenant_b.job, estates.b.table, tier))
    key = _a_data_file(estates.b)

    answered = _refusal(lambda: s3.put_object(Bucket=estates.b.bucket, Key=key, Body=b"a tier's put"))

    assert answered == answer, f"a {tier}-tier credential's put at {key} answered {answered}"


@pytest.mark.parametrize("tier", ["read", "write"])
def test_a_credential_lists_no_sibling_table(tenant_b: Tenant, estates: Estates, tier: str) -> None:
    """Two tables of one tenant in one bucket: a refusal here comes from the policy's prefix, not the bucket's owner."""
    s3 = _client(_vend(tenant_b.job, estates.b.table, tier))

    answered = _refusal(lambda: s3.list_objects_v2(Bucket=estates.b_sibling.bucket, Prefix=f"{estates.b_sibling.prefix}/", MaxKeys=1))

    assert answered in REFUSED, f"a {tier}-tier credential for one table listed its sibling: {answered}"


def test_a_write_credential_puts_nothing_into_a_sibling_table(tenant_b: Tenant, estates: Estates) -> None:
    """Separate from the list leg: a policy can be right for listing and wrong for PUT."""
    s3 = _client(_vend(tenant_b.job, estates.b.table, "write"))

    answered = _refusal(lambda: s3.put_object(Bucket=estates.b_sibling.bucket, Key=_a_data_file(estates.b_sibling), Body=b"sibling"))

    assert answered in REFUSED, f"a write credential for one table put into its sibling: {answered}"


@pytest.mark.parametrize("tier", ["read", "write"])
def test_a_credential_lists_no_table_of_another_tenant(tenant_b: Tenant, estates: Estates, tier: str) -> None:
    s3 = _client(_vend(tenant_b.job, estates.b.table, tier))

    answered = _refusal(lambda: s3.list_objects_v2(Bucket=estates.a.bucket, Prefix=f"{estates.a.prefix}/", MaxKeys=1))

    assert answered in REFUSED, f"tenant B's {tier}-tier credential listed tenant A's table: {answered}"


@pytest.mark.parametrize("tier", ["read", "write"])
def test_a_credential_reads_no_object_of_another_tenant(tenant_a: Tenant, tenant_b: Tenant, estates: Estates, tier: str) -> None:
    """The key is one tenant A's own credential lists, so NoSuchKey could only mean the store let the request in."""
    own = _client(_vend(tenant_a.admin, estates.a.table, "read"))
    key = str(own.list_objects_v2(Bucket=estates.a.bucket, Prefix=f"{estates.a.prefix}/_versions/", MaxKeys=1)["Contents"][0]["Key"])
    s3 = _client(_vend(tenant_b.job, estates.b.table, tier))

    answered = _refusal(lambda: s3.get_object(Bucket=estates.a.bucket, Key=key))

    assert answered in REFUSED, f"tenant B's {tier}-tier credential read tenant A's {key}: {answered}"


def test_a_write_credential_puts_nothing_into_another_tenants_table(tenant_b: Tenant, estates: Estates) -> None:
    """Cross-tenant corruption: tenant B's write credential, aimed at a data-file key under tenant A's table."""
    s3 = _client(_vend(tenant_b.job, estates.b.table, "write"))

    answered = _refusal(lambda: s3.put_object(Bucket=estates.a.bucket, Key=_a_data_file(estates.a), Body=b"cross-tenant"))

    assert answered in REFUSED, f"tenant B's write credential put into tenant A's table: {answered}"
