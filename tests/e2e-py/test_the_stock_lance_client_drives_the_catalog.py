"""The catalog answers the STOCK Lance client, not only rask's own transport.

`open_lakehouse_diff_left.md` § A11, and the estate's headline claim: *the lakehouse is idiomatic
Lance*. That claim is about a client nobody here wrote. Until this suite it was asserted only against
doubles — `RestNamespace` and `namespace_client_impl` appear in `tests/integration/`, both driving a
`TestClient` over a `MagicMock` namespace, and `tests/e2e-py` (the only suite touching a running
catalog) contained **zero** occurrences of either. The NAME of the conformance was present and the
conformance was not, which is the pattern `docs/DECISIONS.md` records six other members of: verify
where a control's value LANDS, not where its name appears.

So this drives `lance_namespace.connect("rest", …)` — resolving to pylance's own Rust-backed
`lance.namespace.RestNamespace`, the thing an outside user gets — against the DEPLOYED catalog, with a
real Dex token. Nothing here imports a rask module: if the estate is idiomatic, a stock client needs
none.

TWO THINGS IT PINS, and the second is the one that decays quietly:

1. **The read surface answers.** Ten operations across namespaces, tables, tags, versions and indices.
2. **A failure arrives as the SPEC'S OWN TYPED ERROR**, carrying its numeric code. This is the half a
   curl cannot check: the wire body may be perfect problem+json while the client still fails to
   reconstruct the typed exception a caller catches. § A5 made nine doors answer their code; this
   proves one of them survives the round trip into `TableTagNotFoundError.code == 8`.

**`headers.Authorization` IS THE CONNECT PROPERTY, and it is not the spec's spelling.** Measured
2026-09-07 against the deployed catalog: `auth_token` — the name the spec's own `Identity` schema uses
for exactly this (`spec.yaml:2452`, "passed via the `Authorization` header with the Bearer scheme") —
is accepted by `connect()` and then silently ignored, and every call answers `UnauthenticatedError`.
So do `bearer_token`, `api_key` and `additional_headers`. Only `headers.Authorization` (or
`header.Authorization`) reaches the wire. That is a pylance client-side quirk rather than a defect in
this estate, and it is recorded HERE because it costs an hour to rediscover and looks exactly like a
broken server.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pyarrow as pa
import pytest
import requests


lance_namespace = pytest.importorskip("lance_namespace")

CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
DEX = os.environ.get("LANCE_E2E_DEX", "http://localhost:5556/dex").rstrip("/")
USER = os.environ.get("LANCE_E2E_USER", "alice@example.com")

#: A namespace and table the deployed estate already holds. Overridable because an estate seeded
#: differently has different names, and a suite that hardcodes one reports a seeding difference as a
#: conformance failure.
NAMESPACE = os.environ.get("LANCE_E2E_STOCK_NAMESPACE", "acme-bronze")
TABLE = os.environ.get("LANCE_E2E_STOCK_TABLE", "agnostic")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.spec_conformance,
    pytest.mark.skipif(not CATALOG, reason="LANCE_E2E_CATALOG_URL not set — a deployed catalog is required"),
]


def _token(user: str) -> str:
    response = requests.post(
        f"{DEX}/token",
        data={
            "grant_type": "password",
            "client_id": "lance-catalog",
            "client_secret": "lance-catalog-secret",
            "username": user,
            "password": "password",
            "scope": "openid",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()["id_token"]


@pytest.fixture(scope="module")
def stock():  # noqa: ANN201 — the stock namespace type is runtime-only
    """The client an outside Lance user gets — no rask import anywhere in its construction."""
    return lance_namespace.connect("rest", {"uri": CATALOG, "headers.Authorization": f"Bearer {_token(USER)}"})


@pytest.fixture(scope="module")
def table_id() -> list[str]:
    return [NAMESPACE, TABLE]


def _requests():  # noqa: ANN202 — the generated request models are runtime-only
    import lance_namespace as ln

    return ln


@pytest.mark.parametrize(
    "operation",
    [
        "list_namespaces",
        "namespace_exists",
        "describe_namespace",
        "list_tables",
        "table_exists",
        "describe_table",
        "count_table_rows",
        "list_table_tags",
        "list_table_versions",
        "list_table_indices",
    ],
)
def test_the_stock_client_drives_the_read_surface(stock, table_id: list[str], operation: str) -> None:  # noqa: ANN001
    """Each op is its own case so a regression names the operation, not "the suite"."""
    ln = _requests()
    calls = {
        "list_namespaces": lambda: stock.list_namespaces(ln.ListNamespacesRequest(id=[])),
        "namespace_exists": lambda: stock.namespace_exists(ln.NamespaceExistsRequest(id=[NAMESPACE])),
        "describe_namespace": lambda: stock.describe_namespace(ln.DescribeNamespaceRequest(id=[NAMESPACE])),
        "list_tables": lambda: stock.list_tables(ln.ListTablesRequest(id=[NAMESPACE])),
        "table_exists": lambda: stock.table_exists(ln.TableExistsRequest(id=table_id)),
        "describe_table": lambda: stock.describe_table(ln.DescribeTableRequest(id=table_id)),
        "count_table_rows": lambda: stock.count_table_rows(ln.CountTableRowsRequest(id=table_id)),
        "list_table_tags": lambda: stock.list_table_tags(ln.ListTableTagsRequest(id=table_id)),
        "list_table_versions": lambda: stock.list_table_versions(ln.ListTableVersionsRequest(id=table_id)),
        "list_table_indices": lambda: stock.list_table_indices(ln.ListTableIndicesRequest(id=table_id)),
    }
    calls[operation]()  # raising IS the failure — a spec op the stock client cannot drive


def test_a_failure_arrives_as_the_SPECS_OWN_TYPED_ERROR(stock, table_id: list[str]) -> None:  # noqa: ANN001
    """The half a curl cannot check.

    A body can be perfect problem+json while the client still fails to rebuild the typed exception a
    caller catches — and `code` is what a generated client dispatches on, never the HTTP status. § A5
    made nine doors answer their code; this proves one survives the round trip into the stock client's
    own exception hierarchy.
    """
    ln = _requests()
    with pytest.raises(ln.TableTagNotFoundError) as caught:
        stock.get_table_tag_version(ln.GetTableTagVersionRequest(id=table_id, tag="no-such-tag-in-any-estate"))
    assert caught.value.code == 8, f"the stock client rebuilt code {caught.value.code}, not the spec's 8"


def test_the_client_is_REALLY_unauthenticated_without_a_credential() -> None:
    """The suite's own control: without the header the same calls must fail, or it proves nothing.

    A conformance suite that would pass against an open door is measuring the door's absence. The
    connect below deliberately uses the spec's `auth_token` spelling, which pylance accepts and
    ignores — so this is simultaneously the control AND the pin on that quirk.
    """
    ln = _requests()
    tokenless = ln.connect("rest", {"uri": CATALOG, "auth_token": _token(USER)})
    with pytest.raises(ln.UnauthenticatedError) as caught:
        tokenless.list_namespaces(ln.ListNamespacesRequest(id=[]))
    assert caught.value.code == 16, f"an uncredentialed call answered code {caught.value.code}, not 16"


def test_the_stock_client_drives_a_WRITE_round_trip(stock) -> None:  # noqa: ANN001
    """create -> insert -> tag -> read the tag -> untag -> drop, all through the stock client.

    The read surface above proves the catalog can be *read* idiomatically; this proves it can be
    *used*. Deliberately ONE scenario rather than independent cases: the steps are a story, each
    depends on the last, and a table that exists only inside it cannot be asserted about separately.

    A fresh uuid-suffixed table per run, dropped at the end, so re-runs never collide and a failure
    leaves at most one small table behind. The row COUNT is the assertion for the insert rather than
    the response's `num_inserted_rows` — the native path leaves that null, so trusting it would make
    this leg pass while nothing was written.
    """
    ln = _requests()
    name = f"stockprobe_{uuid.uuid4().hex[:8]}"
    table = [NAMESPACE, name]
    schema = pa.schema([pa.field("id", pa.int64()), pa.field("s", pa.string())])
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "s": pa.array(["a", "b", "c"])}, schema=schema)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as writer:
        writer.write_table(rows)
    ipc = sink.getvalue().to_pybytes()

    try:
        stock.create_table(ln.CreateTableRequest(id=table), ipc)
        assert stock.count_table_rows(ln.CountTableRowsRequest(id=table)) == 3, "the create did not land its rows"

        stock.create_table_tag(ln.CreateTableTagRequest(id=table, tag="v1", version=1))
        assert stock.get_table_tag_version(ln.GetTableTagVersionRequest(id=table, tag="v1")).version == 1

        stock.insert_into_table(ln.InsertIntoTableRequest(id=table), ipc)
        assert stock.count_table_rows(ln.CountTableRowsRequest(id=table)) == 6, "the insert did not append"

        stock.delete_table_tag(ln.DeleteTableTagRequest(id=table, tag="v1"))
        with pytest.raises(ln.TableTagNotFoundError):
            stock.get_table_tag_version(ln.GetTableTagVersionRequest(id=table, tag="v1"))
    finally:
        with contextlib.suppress(Exception):
            stock.drop_table(ln.DropTableRequest(id=table))
