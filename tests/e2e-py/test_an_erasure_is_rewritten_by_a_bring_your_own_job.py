"""An erasure drill on the live estate: the catalog records the obligation, a bring-your-own job rewrites, the catalog
verifies ([[LH-493]], ADR 0163).

The erasure door records an obligation and commits nothing. A job holding only `writer` and `maintainer` on the
table's namespace, never an owner, makes every rewrite with stock pylance through the namespace, each commit judged at
`version/create`: it deletes the subject by row id, compacts, and drops the table's index, which it rebuilds once the
verification door has released the obligation on the catalog's own reading of storage. The job first leaves the
compaction out, and the obligation stays open naming it.

Two Dex users from the deployed configmap play the parts. The estate admin creates a scratch tenant (a project, a
warehouse with its own bucket, a bronze namespace), owns the table it seeds and asks for the erasure. The job identity
holds `writer` and `maintainer` on the namespace. The tenant is deleted at the end.

Run with `bash scripts/e2e_live.sh tests/e2e-py/test_an_erasure_is_rewritten_by_a_bring_your_own_job.py`, which
discovers every address from the release, or `make e2e-byo-erasure` with the addresses set.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from typing import Any

import boto3
import lance
import lance_namespace
import pyarrow as pa
import pytest
from botocore.config import Config
from lance.optimize import Compaction
from scratch_tenant import CATALOG, DEX, Identity, Tenant, ok, provisioned


LINEAGE = os.environ.get("LANCE_E2E_LINEAGE_URL", "").rstrip("/")
#: How long a committed write may take to reach lineage's feed.
SETTLE_SECONDS = float(os.environ.get("LANCE_E2E_SETTLE_SECONDS", "120"))

#: The identifier being erased, spelled so that a byte search of storage finds nothing else.
SUBJECT = f"subject-{uuid.uuid4().hex[:12]}"
PREDICATE = f"pii = '{SUBJECT}'"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.byo_erasure,
    pytest.mark.skipif(not (CATALOG and LINEAGE and DEX), reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL and LANCE_E2E_DEX (a deployed estate)"),
]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("erasure", grants=("writer", "maintainer"))


def _people(names: list[str]) -> pa.Table:
    return pa.table({"pii": pa.array(names), "note": pa.array([f"note about {name}" for name in names])})


def _verify(identity: Identity, table: str, obligation_id: str) -> dict[str, Any]:
    return ok(identity.http.post(f"{CATALOG}/management/v1/table/{table}/erasure/{obligation_id}/verify", json={"predicate": PREDICATE}, timeout=300))


def _feed_start(identity: Identity) -> int:
    """Where a follower that has read nothing yet starts: the newest row it may see, else the oldest held."""
    body = ok(identity.http.get(f"{LINEAGE}/events", params={"limit": 1}, timeout=30))
    events = body.get("events") or []
    return int(events[0]["seq"]) if events else max(int(body.get("oldest_seq") or 1) - 1, 0)


def _authors_of(identity: Identity, *, since: int, table: str, versions: set[int]) -> dict[int, str]:
    """The `rask_author.sub` of the feed row that committed each of ``versions`` of ``table``, following the feed forward."""
    authors: dict[int, str] = {}
    cursor = since
    deadline = time.monotonic() + SETTLE_SECONDS
    while not versions <= set(authors):
        body = ok(identity.http.get(f"{LINEAGE}/events", params={"since": cursor, "table": table, "limit": 500}, timeout=30))
        for row in body["events"]:
            # A write's facets ride its run; a definition change's, an index built or dropped among them, ride its
            # dataset (`docs/COMMIT-EVENTS.md` § A dataset change).
            event = row.get("event") or {}
            facets = (event.get("run") or event.get("dataset") or {}).get("facets") or {}
            version = (facets.get("rask_lance") or {}).get("version")
            if version is not None:
                authors[int(version)] = str((facets.get("rask_author") or {}).get("sub"))
        cursor = int(body["next_cursor"])
        assert time.monotonic() < deadline, f"the feed for {table} did not show versions {sorted(versions - set(authors))} within {SETTLE_SECONDS:.0f} s"
        if not versions <= set(authors):
            time.sleep(2)
    return {version: authors[version] for version in sorted(versions)}


def _objects_holding(identity: Identity, table_id: list[str], needle: bytes) -> list[str]:
    """Every object under the table's prefix whose bytes hold ``needle``, read on the credential the catalog vends."""
    described = identity.namespace().describe_table(lance_namespace.DescribeTableRequest(id=table_id))
    vended = described.storage_options or {}
    bucket, _, prefix = str(described.location).removeprefix("s3://").partition("/")
    store = boto3.client(
        "s3",
        endpoint_url=vended["endpoint"],
        aws_access_key_id=vended["aws_access_key_id"],
        aws_secret_access_key=vended["aws_secret_access_key"],
        aws_session_token=vended["aws_session_token"],
        region_name=vended.get("region", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
    holding: list[str] = []
    for page in store.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for item in page.get("Contents", []):
            if needle in store.get_object(Bucket=bucket, Key=item["Key"])["Body"].read():
                holding.append(str(item["Key"]).removeprefix(f"{prefix}/"))
    return holding


def test_a_job_rewrites_every_surface_and_the_catalog_releases_what_it_verifies(tenant: Tenant) -> None:
    admin, job = tenant.admin, tenant.job
    table_id = [tenant.bronze, "people"]
    table = "$".join(table_id)
    owner = admin.namespace()
    lance.write_dataset(
        _people(["ann", SUBJECT, "ben"]), namespace_client=owner, table_id=table_id, mode="create", data_storage_version="2.2", enable_stable_row_ids=True
    )
    tenant.tables.append(table)
    lance.write_dataset(_people(["cat", "dan"]), namespace_client=owner, table_id=table_id, mode="append")
    lance.dataset(namespace_client=owner, table_id=table_id).create_scalar_index("pii", index_type="BTREE", name="pii_idx")
    before = lance.dataset(namespace_client=owner, table_id=table_id).version
    start = _feed_start(job)

    # 1. The door records the obligation and commits nothing.
    recorded = admin.http.post(f"{CATALOG}/management/v1/table/{table}/erasure", json={"predicate": PREDICATE}, timeout=120)
    assert recorded.status_code == 202, recorded.text
    obligation = recorded.json()
    assert (obligation["state"], lance.dataset(namespace_client=owner, table_id=table_id).version) == ("open", before)

    # 2. The job deletes by row id and drops the index, and leaves the compaction out: the obligation names it.
    stock = job.namespace()
    head = lance.dataset(namespace_client=stock, table_id=table_id)
    ids = head.to_table(columns=[], filter=PREDICATE, with_row_id=True).column("_rowid").to_pylist()
    head.delete(f"_rowid IN ({', '.join(str(row_id) for row_id in ids)})")
    lance.dataset(namespace_client=stock, table_id=table_id).drop_index("pii_idx")
    owed = _verify(job, table, obligation["obligation_id"])
    failed = [s["surface"] for s in owed["obligation"]["open_surfaces"] if s["outcome"] == "failed" and s["surface"] != "verify"]
    assert (owed["obligation"]["state"], failed) == ("open", ["compact:main"]), owed["obligation"]["open_surfaces"]

    # 3. The job compacts with Lance's own protocol; the catalog releases; the job rebuilds its index over the result.
    head = lance.dataset(namespace_client=stock, table_id=table_id)
    Compaction.commit(head, [task.execute(head) for task in Compaction.plan(head, {"materialize_deletions_threshold": 0.0}).tasks])
    released = _verify(job, table, obligation["obligation_id"])
    assert (released["obligation"]["state"], released["report"]["complete"]) == ("released", True), released["report"]["surfaces"]
    lance.dataset(namespace_client=stock, table_id=table_id).create_scalar_index("pii", index_type="BTREE", name="pii_idx")

    # 4. Every version since the door is the job's own stock commit: the feed names the job as each one's author
    #    (delete, index drop, the compaction's reservation and rewrite, the index rebuild).
    after = lance.dataset(namespace_client=owner, table_id=table_id).version
    authors = _authors_of(job, since=start, table=table, versions=set(range(before + 1, after + 1)))
    assert (len(authors), set(authors.values())) == (5, {job.principal}), authors

    # 5. The listing shows the obligation released, and no object under the table's prefix holds the subject's bytes.
    listed = ok(job.http.get(f"{CATALOG}/management/v1/erasures", params={"limit": 1000}, timeout=30))["obligations"]
    assert [(entry["state"], entry["overdue"]) for entry in listed if entry["obligation_id"] == obligation["obligation_id"]] == [("released", False)]
    assert _objects_holding(admin, table_id, SUBJECT.encode()) == []
    assert sorted(lance.dataset(namespace_client=owner, table_id=table_id).to_table().column("pii").to_pylist()) == ["ann", "ben", "cat", "dan"]
