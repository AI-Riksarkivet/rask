"""#74 — tenant-isolation ATTACK: tenant B's vended credentials must be REFUSED on tenant A's bucket.

open_lakehouse_diff §3 names this "the single most important untested claim in the security story":
byte-PLACEMENT isolation is proven (test_warehouses_e2e.py — A's table lands in bucket-a, absent from
bucket-b), but nothing ever attacked the STS session-policy scoping. Placement proves the catalog
WRITES to the right bucket; it says nothing about whether a credential handed to tenant B can be
pointed at tenant A's bucket by a client that simply ignores the location it was given.

This is an ATTACK test, so it is written from the attacker's side: take the credentials the catalog
really vended for B's table, build a raw S3 client from them, and try A's bucket directly. The
refusal must come from the STORE, not from the catalog — the catalog is not in the request path at
all once a direct credential exists, which is the whole reason a session policy has to be right.

Both tiers, because they fail differently: a read-tier credential that could LIST another tenant's
bucket leaks the estate's shape; a write-tier one that could PUT there is cross-tenant corruption.

NOT mockable — the point is the object store's own evaluation of the session policy. moto's STS
does not enforce inline session policies the way a real store does, so a moto pass would be a lie.
Skipped unless a deployed stack + two tenants' tokens are configured; run via `make e2e-isolation`.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import boto3
import pytest
import requests
from botocore.client import Config
from botocore.exceptions import ClientError


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
TOKEN_A = os.environ.get("LANCE_E2E_TOKEN", "")
TOKEN_B = os.environ.get("LANCE_E2E_TENANT_B_TOKEN", "")
PROJECT_A = os.environ.get("LANCE_E2E_PROJECT", "acme")
PROJECT_B = os.environ.get("LANCE_E2E_PROJECT_B", "")
DELIM = os.environ.get("LANCE_E2E_DELIM", "$")
S3 = os.environ.get("LANCE_E2E_S3", "http://localhost:9900")
ARROW_STREAM = "application/vnd.apache.arrow.stream"

pytestmark = pytest.mark.e2e


def _auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _arrow_ipc() -> bytes:
    import io

    import pyarrow as pa
    import pyarrow.ipc as ipc

    table = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.fixture(scope="module")
def catalog() -> str:
    if not (CATALOG and TOKEN_A and TOKEN_B and PROJECT_B):
        pytest.skip(
            "set LANCE_E2E_CATALOG_URL + LANCE_E2E_TOKEN + LANCE_E2E_TENANT_B_TOKEN + LANCE_E2E_PROJECT_B (two tenants on a deployed, vending-enabled stack)"
        )
    try:
        requests.get(f"{CATALOG}/livez", timeout=5).raise_for_status()
    except Exception:
        pytest.skip("catalog not reachable")
    return CATALOG


def _provision(catalog: str, token: str, project: str, warehouse: str, ns: str, table: str) -> str:
    """One tenant's warehouse → namespace → table. Returns the table's canonical id."""
    r = requests.post(f"{catalog}/v1/warehouses", json={"id": warehouse, "project": project}, headers=_auth(token), timeout=30)
    assert r.status_code in (200, 409), r.text
    r = requests.post(f"{catalog}/v1/warehouses/{warehouse}/namespaces", json={"namespace": ns}, headers=_auth(token), timeout=30)
    assert r.status_code in (200, 409), r.text
    ident = f"{ns}{DELIM}{table}"
    r = requests.post(
        f"{catalog}/v1/table/{ident}/create?mode=overwrite",
        data=_arrow_ipc(),
        headers={**_auth(token), "content-type": ARROW_STREAM},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    return ident


def _vend(catalog: str, token: str, ident: str, tier: str) -> dict[str, str]:
    """The vended ``storage_options`` for this caller's table at this tier.

    Returns the INNER block, not ``body["credentials"]`` (diff2 F5). The response is
    ``{mode, credentials: {storage_options, expires_at_millis}, location, read_version}``, so the
    outer object carries no key material at all — this test read it for its whole life and would have
    raised ``KeyError`` on its first real run. It never did, because the suite is env-gated and skips
    unless a two-tenant deployed stack is configured, which is also why the mismatch survived: a test
    that never runs cannot fail. The unit pin in ``tests/unit/test_vending.py`` exists so the SHAPE is
    now checked by something that runs on every commit.
    """
    r = requests.post(f"{catalog}/v1/table/{ident}/credentials?tier={tier}", headers=_auth(token), timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    if body.get("mode") != "direct":
        pytest.skip(f"stack vends {body.get('mode')} — a session policy is only attackable in direct mode")
    credentials = body.get("credentials") or {}
    options = credentials.get("storage_options")
    assert options, f"vended credentials carried no storage_options: {credentials!r}"
    return options


def _client(creds: dict[str, str]) -> Any:
    """A raw S3 client wearing the vended credentials — the catalog is NOT in this path.

    BARE key names (``access_key_id``, not ``aws_access_key_id``): the vendors emit Lance-style
    options, because `DescribeTableResponse.storage_options` is passed straight to Lance. The
    ``aws_``-prefixed names this used are boto3's OWN kwargs — the mistake was reading the client's
    parameter names back out of the server's payload.

    ``endpoint`` comes from the vended options when present, falling back to the env. The vendor
    includes it whenever the deployment configures one, and preferring it keeps the attack pointed at
    the store the catalog actually issued the credential for.
    """
    return boto3.client(
        "s3",
        endpoint_url=creds.get("endpoint") or S3,
        aws_access_key_id=creds["access_key_id"],
        aws_secret_access_key=creds["secret_access_key"],
        aws_session_token=creds.get("session_token"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        region_name=creds.get("region") or "us-east-1",
    )


@pytest.fixture(scope="module")
def estates(catalog: str) -> dict[str, str]:
    """Two tenants, two warehouses, two buckets — provisioned through the real doors."""
    a = _provision(catalog, TOKEN_A, PROJECT_A, "e2e-iso-a", "isoans", "isoatbl")
    b = _provision(catalog, TOKEN_B, PROJECT_B, "e2e-iso-b", "isobns", "isobtbl")
    return {"a_ident": a, "b_ident": b, "a_bucket": "e2e-iso-a", "b_bucket": "e2e-iso-b"}


@pytest.mark.parametrize("tier", ["read", "write"])
def test_tenant_b_credentials_cannot_list_tenant_a_bucket(catalog: str, estates: dict[str, str], tier: str) -> None:
    """The estate-shape leak. B's credential, pointed at A's bucket, must be refused BY THE STORE."""
    creds = _vend(catalog, TOKEN_B, estates["b_ident"], tier)
    s3 = _client(creds)
    with pytest.raises(ClientError) as exc:
        s3.list_objects_v2(Bucket=estates["a_bucket"], MaxKeys=1)
    code = exc.value.response["Error"]["Code"]
    assert code in ("AccessDenied", "AccessDeniedException", "InvalidAccessKeyId", "SignatureDoesNotMatch"), (
        f"tenant B listed tenant A's bucket with a {tier}-tier credential — the session policy is not scoped: {code}"
    )


@pytest.mark.parametrize("tier", ["read", "write"])
def test_tenant_b_credentials_cannot_read_tenant_a_objects(catalog: str, estates: dict[str, str], tier: str) -> None:
    """Direct object read across the tenant boundary. Refusal must be AccessDenied, never NoSuchKey —
    a NoSuchKey would mean the policy let the request through and only the key was missing."""
    creds = _vend(catalog, TOKEN_B, estates["b_ident"], tier)
    s3 = _client(creds)
    with pytest.raises(ClientError) as exc:
        s3.get_object(Bucket=estates["a_bucket"], Key="isoans/isoatbl.lance/_versions/1.manifest")
    code = exc.value.response["Error"]["Code"]
    assert code != "NoSuchKey", "the store EVALUATED the request against A's bucket — the policy did not scope it"
    assert code in ("AccessDenied", "AccessDeniedException", "InvalidAccessKeyId", "SignatureDoesNotMatch"), code


def test_tenant_b_write_credentials_cannot_put_into_tenant_a_bucket(catalog: str, estates: dict[str, str]) -> None:
    """The corruption case, and the one that matters most: a WRITE credential must not be able to
    plant an object in another tenant's bucket. Asserted separately from read because a policy can
    be right for GET and wrong for PUT."""
    creds = _vend(catalog, TOKEN_B, estates["b_ident"], "write")
    s3 = _client(creds)
    key = f"isoans/isoatbl.lance/data/{uuid.uuid4().hex}.lance"
    with pytest.raises(ClientError) as exc:
        s3.put_object(Bucket=estates["a_bucket"], Key=key, Body=b"cross-tenant")
    code = exc.value.response["Error"]["Code"]
    assert code in ("AccessDenied", "AccessDeniedException", "InvalidAccessKeyId", "SignatureDoesNotMatch"), (
        f"tenant B WROTE into tenant A's bucket: {code} — cross-tenant corruption is reachable"
    )


@pytest.mark.parametrize("tier", ["read", "write"])
def test_the_credential_still_works_on_its_OWN_table(catalog: str, estates: dict[str, str], tier: str) -> None:
    """The negative twin, and the reason this suite cannot pass vacuously: a credential that is
    refused EVERYWHERE would satisfy every assertion above while breaking the product. B's own
    table must remain readable with B's own credential."""
    creds = _vend(catalog, TOKEN_B, estates["b_ident"], tier)
    s3 = _client(creds)
    listing = s3.list_objects_v2(Bucket=estates["b_bucket"], Prefix="isobns", MaxKeys=5)
    assert listing.get("KeyCount", 0) > 0, "B cannot read B's own table — the session policy is too tight"


def test_read_tier_credentials_cannot_write_to_their_OWN_table(catalog: str, estates: dict[str, str]) -> None:
    """The tier split is a security boundary too, not just an ergonomic one: a READ credential for
    the caller's own table must not be able to PUT into it."""
    creds = _vend(catalog, TOKEN_B, estates["b_ident"], "read")
    s3 = _client(creds)
    key = f"isobns/isobtbl.lance/data/{uuid.uuid4().hex}.lance"
    with pytest.raises(ClientError) as exc:
        s3.put_object(Bucket=estates["b_bucket"], Key=key, Body=b"read-tier-should-not-write")
    assert exc.value.response["Error"]["Code"] in ("AccessDenied", "AccessDeniedException"), (
        "a read-tier credential could WRITE — the tier split is not enforced at the store"
    )
