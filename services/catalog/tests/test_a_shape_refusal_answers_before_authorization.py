"""[[LH-526]] A request that names nothing a door can name is refused 400 before any permission is asked (ADR 0173 §4).

Every door judges a request in one order: who is asking (401), whether the request is well formed (400), whether the
caller may make it (403). A malformed request is the caller's mistake whatever they hold, and a 403 sends them to ask
for a grant that no grant can give. The router judges two shapes before authorization: a `delimiter` other than the
spec's (`lance_docs/ns_catalog/spec.yaml:2314-2318`, "When not specified, the `$` delimiter must be used"), and a segment
a naming door is handed that is not a name, in its path, a rename's destination or a batch's ids. Bob holds no grant on
anything here, so each refusal below is one he would read as his 403 if authorization came first.

The delimiter is the spec's, and no setting moves it: with `LANCE_NS_DELIMITER` at `.` in the catalog's environment,
it splits identifiers on `$` and refuses a client naming `.`.

Every decision is the real model's, on a store of this test's own ([[LH-235]]).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The spec's code for a malformed request (`lance_docs/ns_catalog/spec.yaml`), which a stock client reads.
INVALID_INPUT = 13


@pytest.fixture
def estate(governed_catalog: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    """`db1` with the table `db1$t`, both alice's, and the old delimiter setting at `.` from here on."""
    alice = governed_catalog.bearer("alice")
    client = governed_catalog.client
    assert client.post("/v1/namespace/db1/create", json={}, headers=alice).status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))
    created = client.post("/v1/table/db1$t/create", content=rows, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    # Settings are read per request, so the next request is served under this environment.
    monkeypatch.setenv("LANCE_NS_DELIMITER", ".")
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    return governed_catalog


@pytest.mark.parametrize(
    ("door", "body", "named"),
    [
        pytest.param("/v1/table/db1$t/describe?delimiter=.", {}, "'$'", id="a-delimiter-other-than-the-spec's"),
        pytest.param("/v1/table/db1$t%01/declare", {}, "control character", id="a-path-id-that-is-not-a-name"),
        pytest.param("/v1/table/db1$t/rename", {"new_table_name": " t2"}, "whitespace", id="a-rename-destination-that-is-not-a-name"),
        pytest.param("/v1/table/batch-commit", {"operations": [{"declare_table": {"id": ["db1", "t/2"]}}]}, "'/'", id="a-batch-id-that-is-not-a-name"),
    ],
)
def test_a_request_naming_what_no_door_can_name_is_refused_before_authorization(estate: Any, door: str, body: dict[str, Any], named: str) -> None:
    refused = estate.client.post(door, json=body, headers=estate.bearer("bob"))

    assert (refused.status_code, refused.headers["content-type"]) == (400, "application/problem+json"), refused.text
    assert refused.json()["code"] == INVALID_INPUT, refused.text
    assert named in refused.json()["detail"], refused.text
