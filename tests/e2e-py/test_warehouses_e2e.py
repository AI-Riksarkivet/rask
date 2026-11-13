"""A warehouse is the first segment of every name, read back on the DEPLOYED catalog ([[LH-593]], #3-A).

A project holds two warehouses, each on a bucket the catalog creates at RUNTIME through the API (not the chart's bucket
hook). On that pair the module holds:

- a table created in a namespace of the first warehouse lands in the first bucket and is ABSENT from the second;
- the two warehouses each hold a namespace of the same name, `sales`, with a table `t`, and each table is read only by
  the person granted on its own warehouse's namespace;
- a caller holding nothing on a warehouse is answered alike for a name that exists in it, a name that does not, and a
  warehouse id no record holds: the same status, problem code and body, naming nothing the caller did not send;
- no binding record stands on the control root once the module's namespaces are made: a namespace is reached by its
  name's first segment, never through a record of which warehouse holds it;
- a stock client (`lance_namespace.connect("rest", ...)`) lists, creates, writes and reads `<warehouse>$events$t`
  through the spec's own `/v1` doors;
- the root's `ListNamespaces` shows each caller the module's warehouses it may see and no other of them.

Both warehouses are this module's own, made per run in a scratch project (`scratch_tenant`) and removed with their
buckets at the end, so no run leaves a warehouse on the estate and no shared project gains one while it runs. The
buckets and the control root are listed as the suites' own store identity, whose token the runner hands it and whose
policy lists every bucket (`deploy/values-e2e-suites.yaml`). Run via ``bash scripts/e2e_live.sh tests/e2e-py/test_warehouses_e2e.py``.

WHAT IS NOT CLAIMED. The root listing is held exact over the warehouses this module made: the shared estate's other
warehouses, and what each person holds on them, are other runs' and the seed's, so they are left out of the comparison.
That no binding record is READ is not observable from outside the catalog; the module holds that none stands to be read.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator

import lance
import lance_namespace as ln
import pyarrow as pa
import pyarrow.fs as pafs
import pytest
from lance_namespace import ErrorCode
from pydantic import BaseModel, ConfigDict
from refusals import assert_refused, body_of, described
from scratch_tenant import CATALOG, DEX, Identity, Tenant, ok, provisioned
from store_identity import store_options

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.problem import PROBLEM_JSON
from lakehouse.storage_options import s3_filesystem


S3 = os.environ.get("LANCE_E2E_S3", "")
#: The person granted on the second warehouse's namespace: a Dex user of the deployed configmap who holds nothing in the
#: scratch project.
OTHER_USER = os.environ.get("LANCE_E2E_VALIDATOR_USER", "bob@example.com")
#: The namespace name both warehouses hold.
SAME_NAME = "sales"
#: How long a grant written to the store may take to reach every check.
SETTLE_SECONDS = float(os.environ.get("LANCE_E2E_SETTLE_SECONDS", "120"))

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (CATALOG and DEX and S3), reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX and LANCE_E2E_S3 (a deployed estate)"),
]


def _list_bucket(bucket: str) -> list[str]:
    fs = s3_filesystem(store_options(S3))
    return [info.path for info in fs.get_file_info(pafs.FileSelector(bucket, allow_not_found=True, recursive=True))]


def _rows(count: int) -> bytes:
    return encode_arrow_stream(pa.table({"id": pa.array(list(range(count)), pa.int64())}))


def _counted(identity: Identity, table: str) -> tuple[int, int | None]:
    """``table``'s row count as ``identity`` asks the catalog: the status, and the count or the refusal's spec code."""
    answer = identity.http.post(f"{CATALOG}/v1/table/{table}/count_rows", json={}, timeout=60)
    if answer.status_code == 200:
        return 200, int(answer.text)
    return answer.status_code, json.loads(body_of(answer)).get("code")


class SameNames(BaseModel):
    """The two warehouses' `sales$t`, and who is granted `reader` on each warehouse's `sales`."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    first: str
    second: str
    readers: dict[str, Identity]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("whiso", grants=())


@pytest.fixture(scope="module")
def second_warehouse(tenant: Tenant) -> Iterator[str]:
    """A second warehouse of the scratch project, on a bucket of its own; deleted with its bucket at the end."""
    warehouse = f"{tenant.project}-b"
    answer = ok(tenant.admin.http.post(f"{CATALOG}/management/v1/warehouses", json={"id": warehouse, "project": tenant.project}, timeout=30))
    assert answer["root_uri"] == f"s3://{warehouse}", f"a second warehouse was not given a bucket of its own: {answer}"
    try:
        yield warehouse
    finally:
        params = {"cascade": "true", "purge_bucket": "true"}
        ok(tenant.admin.http.delete(f"{CATALOG}/management/v1/warehouses/{warehouse}", params=params, timeout=120))


@pytest.fixture(scope="module")
def same_names(tenant: Tenant, second_warehouse: str) -> SameNames:
    """`sales$t` in each warehouse, one row in the first and two in the second, made by the estate admin, with `reader`
    on the first's `sales` granted to the tenant's job identity and on the second's to the other person. The second
    warehouse's delete takes its table; the tenant's teardown drops the first's."""
    admin, other = tenant.admin, Identity(OTHER_USER)
    readers = {tenant.project: tenant.job, second_warehouse: other}
    for rows, (warehouse, reader) in enumerate(readers.items(), start=1):
        namespace = f"{warehouse}${SAME_NAME}"
        ok(admin.http.post(f"{CATALOG}/v1/namespace/{namespace}/create", json={}, timeout=60))
        table = f"{namespace}$t"
        ok(admin.http.post(f"{CATALOG}/v1/table/{table}/create", data=_rows(rows), headers={"content-type": ARROW_STREAM_MEDIA_TYPE}, timeout=60))
        if warehouse == tenant.project:
            tenant.tables.append(table)
        ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{namespace}/access/grant", json={"user": reader.principal, "relation": "reader"}, timeout=60))
        deadline = time.monotonic() + SETTLE_SECONDS
        while _counted(reader, table) != (200, rows):
            assert time.monotonic() < deadline, f"{reader.principal}'s grant on {namespace} was not seen within {SETTLE_SECONDS:.0f} s"
            time.sleep(1)
    return SameNames(first=tenant.project, second=second_warehouse, readers=readers)


def _root_listing(identity: Identity) -> list[str]:
    """Every warehouse the root's `ListNamespaces` shows ``identity``, page by page."""
    found: list[str] = []
    params: dict[str, str] = {}
    while True:
        page = ok(identity.http.get(f"{CATALOG}/v1/namespace/$/list", params=params, timeout=60))
        found += page["namespaces"]
        if not page.get("page_token"):
            return sorted(found)
        params["page_token"] = str(page["page_token"])


def test_a_table_lands_in_its_warehouses_bucket_and_in_no_other(tenant: Tenant, second_warehouse: str) -> None:
    table = f"{tenant.bronze}$e2etbl"

    created = ok(
        tenant.admin.http.post(
            f"{CATALOG}/v1/table/{table}/create",
            data=encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())})),
            headers={"content-type": ARROW_STREAM_MEDIA_TYPE},
            timeout=60,
        )
    )
    tenant.tables.append(table)

    # The table lives where its create answered: `<namespace root>/<table uuid>` (ADR 0173 §5), so the uuid directory is
    # what each bucket is searched for.
    bucket, _, key = str(created["location"]).removeprefix("s3://").partition("/")
    assert bucket == tenant.project, f"a table under a namespace of warehouse {tenant.project} was located in {bucket!r}"
    assert any(path.startswith(f"{bucket}/{key}/_versions/") for path in _list_bucket(bucket)), f"no manifest of {table} in {bucket}"
    assert not any(key in path for path in _list_bucket(second_warehouse)), f"{table}'s bytes reached the other warehouse's bucket"


def test_two_warehouses_hold_the_same_name_each_read_only_by_its_own_grant_holders(same_names: SameNames) -> None:
    warehouses = (same_names.first, same_names.second)

    read = {(held, asked): _counted(same_names.readers[held], f"{asked}${SAME_NAME}$t") for held in warehouses for asked in warehouses}

    refused = (403, ErrorCode.PERMISSION_DENIED)
    first, second = warehouses
    assert read == {(first, first): (200, 1), (first, second): refused, (second, first): refused, (second, second): (200, 2)}


def test_a_caller_holding_nothing_on_a_warehouse_is_answered_alike_for_any_name_in_it(tenant: Tenant, same_names: SameNames) -> None:
    """The job holds nothing on the second warehouse. Its describe of the second's `sales$t`, of a table the second does
    not hold, and of `sales$t` in a warehouse no record holds each answers the same refusal, which tells it nothing about
    which of the three exists."""
    held = f"{same_names.second}${SAME_NAME}$t"
    sent = {"held": held, "absent": f"{same_names.second}${SAME_NAME}$absent", "no-warehouse": f"{tenant.project}-none${SAME_NAME}$t"}
    admin = dict(tenant.admin.http.headers)
    standing = described(CATALOG, held, admin)
    foreign = {standing["location"], standing["properties"]["rask.uuid"], tenant.admin.principal, same_names.readers[same_names.second].principal}

    answers = {
        case: assert_refused(
            lambda name=name: tenant.job.http.post(f"{CATALOG}/v1/table/{name}/describe", json={}, timeout=60),
            status=403,
            code=ErrorCode.PERMISSION_DENIED,
            unchanged=lambda: described(CATALOG, held, admin),
            foreign=foreign,
        )
        for case, name in sent.items()
    }

    def shape(case: str) -> tuple[str, str]:
        """The answer's media type, and its body with the id the caller sent standing in for itself and the occurrence id left out."""
        body = {key: value for key, value in answers[case].json().items() if key != "instance"}
        return answers[case].headers["content-type"], json.dumps(body, sort_keys=True).replace(sent[case], "<sent>")

    held_shape = shape("held")
    assert {case: shape(case) for case in sent} == dict.fromkeys(sent, (PROBLEM_JSON, held_shape[1]))


def test_a_stock_client_lists_creates_writes_and_reads_a_table_in_a_warehouse(tenant: Tenant, same_names: SameNames) -> None:
    client = tenant.admin.namespace()
    namespace = [same_names.first, "events"]
    table = [*namespace, "t"]

    client.create_namespace(ln.CreateNamespaceRequest(id=namespace))
    lance.write_dataset(
        pa.table({"id": pa.array([0, 1], pa.int64())}),
        namespace_client=client,
        table_id=table,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    tenant.tables.append("$".join(table))
    lance.write_dataset(pa.table({"id": pa.array([2], pa.int64())}), namespace_client=client, table_id=table, mode="append")
    namespaces = sorted(client.list_namespaces(ln.ListNamespacesRequest(id=[same_names.first])).namespaces)
    tables = client.list_tables(ln.ListTablesRequest(id=namespace)).tables
    ids = sorted(lance.dataset(namespace_client=client, table_id=table).to_table(columns=["id"]).column("id").to_pylist())

    assert (namespaces, tables, ids) == (["bronze", "events", SAME_NAME], ["t"], [0, 1, 2])


def test_the_root_lists_each_caller_the_warehouses_it_may_see(tenant: Tenant, same_names: SameNames) -> None:
    made = {same_names.first, same_names.second}
    callers = {"admin": tenant.admin, "first-reader": same_names.readers[same_names.first], "second-reader": same_names.readers[same_names.second]}

    seen = {who: sorted(set(_root_listing(identity)) & made) for who, identity in callers.items()}

    assert seen == {"admin": sorted(made), "first-reader": [same_names.first], "second-reader": [same_names.second]}
