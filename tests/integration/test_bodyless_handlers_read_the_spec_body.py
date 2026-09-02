"""The eight remaining spec ops must read their REQUIRED JSON request body.

The Lance Namespace spec declares a REQUIRED `application/json` body and NO query parameters for
`DescribeNamespace`, `NamespaceExists`, `TableExists`, `DeregisterTable`, `DescribeTransaction`,
`ListTableIndices`, `DescribeTableIndexStats` and `GetTableStats` (verified against the vendored
`lance_docs/ns_catalog/spec.yaml`: every one has `requestBody.required: true` and `query=[]`). Every
shipped client — pylance's Rust `RestNamespace`, lancedb `namespace_client_impl="rest"`, lance-ray in
namespace mode, and the generated urllib3 client — sends those fields in the body.

rask declared none of them, so FastAPI bound rask's own aliases as QUERY parameters and dropped the
body on the floor. `DescribeTable` was fixed in `4c64046c`; these eight were not, which is blocker A1
in `open_lakehouse_diff_left.md`.

TWO CONSEQUENCES, and the second is why this test exists now. A spec client's `version`, `branch`,
`page_token` and `limit` were silently ignored — an unpaged listing, main's stats, the latest version.
And it reopened a hole in the branch work of 2026-09-01: `stats`, `index/list` and `index/{n}/stats`
were given a `branch` QUERY parameter so they could REFUSE a branch-scoped read, but a spec client
sends `branch` in the body, so the refusal never fired and the door answered from main exactly as
before. A guard on one of two channels is not a guard — the same lesson `explain_plan` and `describe`
each taught in turn.

THE PRECEDENCE RULE, matching `describe_table`: a field PRESENT in the body wins; rask's query aliases
remain as a fallback so existing callers keep working. `reconcile_body_id` runs on every one of them,
so a body `id` that contradicts the path is a 400 rather than silently ignored (spec duality rule).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import (
    DeregisterTableResponse,
    DescribeNamespaceResponse,
    DescribeTableIndexStatsResponse,
    DescribeTransactionResponse,
    GetTableStatsResponse,
    ListTableIndicesResponse,
)
from lance_namespace_urllib3_client.models.fragment_stats import FragmentStats
from lance_namespace_urllib3_client.models.fragment_summary import FragmentSummary


def _sent(mock: MagicMock) -> Any:
    """The request model the ROUTE handed the backend."""
    return mock.call_args[0][0]


# --- the ops whose body carries an identifier only -------------------------------------------------


def test_describe_namespace_reconciles_a_body_id(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse()
    assert client.post("/v1/namespace/db/describe", json={"id": ["db"]}).status_code == 200
    assert _sent(fake_ns.describe_namespace).id == ["db"]


def test_describe_namespace_refuses_a_contradicting_body_id(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse()
    resp = client.post("/v1/namespace/db/describe", json={"id": ["other"]})
    assert resp.status_code == 400, f"a body id contradicting the path must be 400 (spec duality): {resp.status_code}"


def test_namespace_exists_reconciles_a_body_id(client: TestClient, fake_ns: MagicMock) -> None:
    assert client.post("/v1/namespace/db/exists", json={"id": ["db"]}).status_code in (200, 204)
    assert _sent(fake_ns.namespace_exists).id == ["db"]


def test_namespace_exists_refuses_a_contradicting_body_id(client: TestClient, fake_ns: MagicMock) -> None:
    assert client.post("/v1/namespace/db/exists", json={"id": ["other"]}).status_code == 400


def test_describe_transaction_reconciles_a_body_id(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_transaction.return_value = DescribeTransactionResponse(status="open")
    assert client.post("/v1/transaction/tx1/describe", json={"id": ["tx1"]}).status_code == 200
    assert _sent(fake_ns.describe_transaction).id == ["tx1"]


def test_describe_transaction_refuses_a_contradicting_body_id(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_transaction.return_value = DescribeTransactionResponse(status="open")
    assert client.post("/v1/transaction/tx1/describe", json={"id": ["other"]}).status_code == 400


# --- the ops whose body carries a TARGET, where dropping it answers about the wrong thing ----------


def test_table_exists_honours_a_body_version(client: TestClient, fake_ns: MagicMock) -> None:
    """`TableExistsRequest.version` asks whether the table exists AT a version."""
    client.post("/v1/table/db$t/exists", json={"version": 3})
    assert _sent(fake_ns.table_exists).version == 3, "the body's version was dropped, so the answer is about the latest version"


def test_list_table_indices_honours_body_paging(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.list_table_indices.return_value = ListTableIndicesResponse(indexes=[])
    assert client.post("/v1/table/db$t/index/list", json={"limit": 1, "page_token": "p2"}).status_code == 200
    sent = _sent(fake_ns.list_table_indices)
    assert (sent.limit, sent.page_token) == (1, "p2"), "an unpaged listing was returned for a paged request"


def test_list_table_indices_refuses_a_body_branch(client: TestClient, fake_ns: MagicMock) -> None:
    """THE REOPENED HOLE. The 501 refusal was wired to the query channel only."""
    fake_ns.list_table_indices.return_value = ListTableIndicesResponse(indexes=[])
    resp = client.post("/v1/table/db$t/index/list", json={"branch": "work"})
    assert resp.status_code == 501, f"a branch in the BODY was answered {resp.status_code} from main; the query-channel refusal never fired"


def test_index_stats_refuses_a_body_branch(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table_index_stats.return_value = DescribeTableIndexStatsResponse(num_indexed_rows=0, num_unindexed_rows=0)
    resp = client.post("/v1/table/db$t/index/i1/stats", json={"branch": "work"})
    assert resp.status_code == 501, f"{resp.status_code}: {resp.text[:160]}"


def test_get_table_stats_refuses_a_body_branch(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.get_table_stats.return_value = GetTableStatsResponse(
        total_bytes=0,
        num_rows=0,
        num_indices=0,
        fragment_stats=FragmentStats(num_fragments=0, num_small_fragments=0, lengths=FragmentSummary(min=0, max=0, mean=0, p25=0, p50=0, p75=0, p99=0)),
    )
    resp = client.post("/v1/table/db$t/stats", json={"branch": "work"})
    assert resp.status_code == 501, f"{resp.status_code}: {resp.text[:160]}"


def test_deregister_table_reconciles_a_body_id(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.deregister_table.return_value = DeregisterTableResponse()
    assert client.post("/v1/table/db$t/deregister", json={"id": ["db", "t"]}).status_code == 200
    assert _sent(fake_ns.deregister_table).id == ["db", "t"]


def test_deregister_table_refuses_a_contradicting_body_id(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.deregister_table.return_value = DeregisterTableResponse()
    assert client.post("/v1/table/db$t/deregister", json={"id": ["db", "other"]}).status_code == 400


# --- the query aliases must keep working, or this "fix" is a break ---------------------------------


@pytest.mark.parametrize(
    ("path", "query", "attr", "value"),
    [
        ("/v1/table/db$t/index/list", {"limit": 2}, "limit", 2),
        ("/v1/table/db$t/index/list", {"page_token": "q"}, "page_token", "q"),
    ],
)
def test_the_query_alias_still_works(client: TestClient, fake_ns: MagicMock, path: str, query: dict[str, Any], attr: str, value: Any) -> None:
    fake_ns.list_table_indices.return_value = ListTableIndicesResponse(indexes=[])
    assert client.post(path, params=query).status_code == 200
    assert getattr(_sent(fake_ns.list_table_indices), attr) == value
