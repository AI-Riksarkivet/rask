"""[[LH-526]] Every door that names a table or namespace refuses a segment that is not a name, 400 (ADR 0173 §4).

A name is Unicode NFC text of 1 to 255 characters holding no `$`, `+`, `/` or control character and neither beginning
nor ending with whitespace; it is refused, never rewritten. The rule is judged once, by the router's shape rung, on what
each door is asked to name: a making door's path id (create, declare, register, namespace and view create), a rename's
destination and a batch's ids. Each case below pairs one door with one shape, so every shape and every door is driven,
and a refused request makes and moves nothing.

`*` and `?` are names, since no name reaches a credential's policy: a table the catalog makes lives under its uuid.

A path can spell two shapes a body cannot. `%2B` is a literal `+`, refused, where a bare `+` is the space pylance's
Rust client sends; and `%2F` is a `/` that no route can carry, refused before routing rather than answered as a
missing route.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The spec's code for a malformed request (`lance_docs/ns_catalog/spec.yaml`), which a stock client reads.
INVALID_INPUT = 13
ARROW = {"content-type": ARROW_STREAM_MEDIA_TYPE}
ROWS = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))


@pytest.fixture
def wc(real_ns_client: TestClient) -> TestClient:
    """The namespace `wc` holding the one table `wc$src`, which a rename moves."""
    assert real_ns_client.post("/v1/namespace/wc/create", json={}).status_code == 200
    created = real_ns_client.post("/v1/table/wc$src/create", content=ROWS, headers=ARROW)
    assert created.status_code == 200, created.text
    return real_ns_client


def _send(client: TestClient, door: str, body: dict[str, Any] | None) -> Any:  # noqa: ANN401 — an httpx Response
    """POST ``door``: an Arrow stream to a create door, else ``body`` as JSON."""
    if door.startswith("/v1/table/") and door.endswith("/create"):
        return client.post(door, content=ROWS, headers=ARROW)
    return client.post(door, json=body or {})


@pytest.mark.parametrize(
    ("door", "body", "named"),
    [
        pytest.param("/v1/table/wc$e%CC%81/create", None, "Unicode NFC", id="create-a-name-not-in-nfc"),
        pytest.param("/v1/table/wc$t%01/declare", None, "control character", id="declare-a-control-character"),
        pytest.param("/v1/table/wc$$t/register", {"location": "elsewhere"}, "at least one character", id="register-an-empty-segment"),
        pytest.param(f"/v1/namespace/wc${'n' * 256}/create", None, "at most 255 characters", id="namespace-create-256-characters"),
        pytest.param("/v1/namespace/wc$a%2Bb/create", None, "'+'", id="namespace-create-a-literal-plus"),
        pytest.param("/v1/materialized_view/wc$mv%20/create", None, "whitespace", id="view-create-trailing-whitespace"),
        pytest.param("/v1/table/wc$a%2Fb/create", None, "'/'", id="create-an-encoded-slash"),
        pytest.param("/v1/table/wc$src/rename", {"new_table_name": "a$b"}, "'$'", id="rename-the-delimiter-in-the-name"),
        pytest.param("/v1/table/wc$src/rename", {"new_table_name": "a/b"}, "'/'", id="rename-a-slash-in-the-name"),
        pytest.param(
            "/v1/table/wc$src/rename", {"new_namespace_id": [" wc"], "new_table_name": "b"}, "whitespace", id="rename-into-a-namespace-that-is-not-a-name"
        ),
        pytest.param(
            "/v1/table/batch-commit", {"operations": [{"declare_table": {"id": ["wc", ""]}}]}, "at least one character", id="batch-commit-an-empty-id"
        ),
        pytest.param("/v1/table/version/batch-create", {"entries": [{"id": ["wc", "t\u0085"]}]}, "control character", id="batch-create-a-c1-control"),
    ],
)
def test_a_naming_door_refuses_a_segment_that_is_not_a_name(wc: TestClient, door: str, body: dict[str, Any] | None, named: str) -> None:
    refused = _send(wc, door, body)

    assert (refused.status_code, refused.headers["content-type"]) == (400, "application/problem+json"), refused.text
    assert refused.json()["code"] == INVALID_INPUT, refused.text
    assert named in refused.json()["detail"], refused.text
    assert wc.get("/v1/namespace/wc/table/list").json()["tables"] == ["src"], "a refused request made or moved a table"
    assert wc.get("/v1/namespace/wc/list").json()["namespaces"] == [], "a refused request made a namespace"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("a*b?c", id="iam-wildcards"),
        pytest.param("sp ace", id="a-space-between-characters"),
        pytest.param("x" * 255, id="255-characters"),
    ],
)
def test_a_name_holds_any_other_character(wc: TestClient, name: str) -> None:
    """The other side of the rule: what no name reaches makes any name, up to the length bound."""
    path = f"/v1/table/wc${name.replace(' ', '%20').replace('?', '%3F')}"

    declared = wc.post(f"{path}/declare", json={})

    assert declared.status_code == 200, declared.text
    assert name in wc.get("/v1/namespace/wc/table/list", params={"include_declared": "true"}).json()["tables"]
