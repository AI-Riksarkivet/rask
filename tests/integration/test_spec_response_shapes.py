"""Three ops answer a shape the reference client cannot parse (blocker A2).

Measured against the vendored spec (`lance_docs/ns_catalog/spec.yaml`, `components.responses`):

| op | spec 200 | rask |
| --- | --- | --- |
| `CountTableRows` | `application/json`, a bare integer | `text/plain` |
| `ExplainTableQueryPlan` | `application/json`, a JSON string | `text/plain` |
| `AnalyzeTableQueryPlan` | `application/json`, a JSON string | `text/plain` |
| `UpdateTableSchemaMetadata` | `application/json`, a direct `{str: str}` map | the wrapped `{metadata, transaction_id}` envelope |

CONSEQUENCE, verified upstream by the conformance pass: pylance's Rust `RestNamespace` (and lancedb
`namespace_client_impl="rest"`, which resolves to it) raises `InternalError: Failed to parse response:
invalid type: map, expected a string` on the metadata write — after the write has already committed —
and `expected value` on both plan ops, because the 0.12.0 reqwest client rejects `text/plain` outright.
`count_rows` survives only by accident: a bare number happens to parse as JSON.

The Python urllib3 client tolerates text/plain, which is exactly why rask's own e2e never noticed.

THE ENVELOPE IS NOT LOST, it moves. `transaction_id` and the null-deletes dialect are rask extensions;
under R2 they belong on the management API, not on a spec route whose response a stock client must be
able to deserialise.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from lance_namespace import UpdateTableSchemaMetadataResponse


def _json_media(resp: object) -> str:
    return (getattr(resp, "headers", {}) or {}).get("content-type", "")


def test_count_rows_answers_a_json_integer(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.count_table_rows.return_value = 7
    resp = client.post("/v1/table/db$t/count_rows", json={})
    assert resp.status_code == 200, resp.text
    assert "application/json" in _json_media(resp), f"spec declares application/json; got {_json_media(resp)!r}"
    assert resp.json() == 7, "the spec's bare integer, not a string"


def test_explain_plan_answers_a_json_string(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.explain_table_query_plan.return_value = "ProjectionExec: expr=[id@0]"
    resp = client.post("/v1/table/db$t/explain_plan", json={"query": {"vector": {"single_vector": [1.0]}, "k": 1}})
    assert resp.status_code == 200, resp.text
    assert "application/json" in _json_media(resp), f"the 0.12.0 reqwest client rejects text/plain outright; got {_json_media(resp)!r}"
    assert resp.json() == "ProjectionExec: expr=[id@0]", "a JSON string, so serde parses it"


def test_analyze_plan_answers_a_json_string(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.analyze_table_query_plan.return_value = "AnalyzeExec: metrics=[]"
    resp = client.post("/v1/table/db$t/analyze_plan", json={"vector": {"single_vector": [1.0]}, "k": 1})
    assert resp.status_code == 200, resp.text
    assert "application/json" in _json_media(resp)
    assert resp.json() == "AnalyzeExec: metrics=[]"


def test_schema_metadata_update_answers_the_direct_map(client: TestClient, fake_ns: MagicMock) -> None:
    """The REST-only rule: the body IS the updated metadata map, not an envelope around it."""
    fake_ns.update_table_schema_metadata.return_value = UpdateTableSchemaMetadataResponse(metadata={"owner": "ana"}, transaction_id="tx")
    resp = client.post("/v1/table/db$t/schema_metadata/update", json={"metadata": {"owner": "ana"}})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"owner": "ana"}, (
        f"the wrapped envelope is what makes the Rust client raise 'invalid type: map, expected a string' AFTER the write commits; got {resp.json()!r}"
    )


def test_a_plan_string_containing_json_still_round_trips(client: TestClient, fake_ns: MagicMock) -> None:
    """A plan is opaque text. Encoding it as a JSON string must not let a plan that LOOKS like JSON be
    mistaken for structure — the failure a naive `Response(content=plan)` would introduce."""
    plan = '{"not": "structure"}'
    fake_ns.explain_table_query_plan.return_value = plan
    resp = client.post("/v1/table/db$t/explain_plan", json={"query": {"vector": {"single_vector": [1.0]}, "k": 1}})
    assert resp.json() == plan
    assert json.loads(resp.text) == plan
