"""Integration tests for *our* REST layer.

The namespace backend is faked, so every assertion is about code we wrote:
identifier parsing from the path, routing to the correct operation, request
assembly (query params / headers / body), response serialization and
content-types, and error → HTTP / Problem-Details mapping. No lance operations
are exercised.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import lance
import pyarrow as pa
from fastapi.testclient import TestClient
from lance_namespace import (
    BatchDeleteTableVersionsResponse,
    CreateNamespaceResponse,
    CreateTableResponse,
    CreateTableVersionResponse,
    DescribeTableResponse,
    DescribeTableVersionResponse,
    ListNamespacesResponse,
    ListTableVersionsResponse,
    MergeInsertIntoTableResponse,
    TableAlreadyExistsError,
    TableNotFoundError,
    TableVersion,
    TableVersionNotFoundError,
)


ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}


# --- identifier parsing (our logic) ---------------------------------------- #


def test_table_id_is_parsed_from_path(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    client.post("/v1/table/db1$users/describe")
    assert fake_ns.describe_table.call_args.args[0].id == ["db1", "users"]


def test_body_id_matching_the_path_passes(client: TestClient, fake_ns: MagicMock) -> None:
    # Spec (operations/index.md): a body-level id may restate the path id — identical is fine.
    fake_ns.count_table_rows.return_value = 7
    resp = client.post("/v1/table/db1$users/count_rows", json={"id": ["db1", "users"]})
    assert resp.status_code == 200, resp.text
    assert fake_ns.count_table_rows.call_args.args[0].id == ["db1", "users"]


def test_body_id_differing_from_the_path_is_400(client: TestClient, fake_ns: MagicMock) -> None:
    # CONTRACT (spec; docs/DECISIONS.md "FEATURE-GAP minor deviations" #1): a body id that
    # CONTRADICTS the path id must refuse —
    # the path id is what the authz gate checked, so silently picking either one is wrong.
    resp = client.post("/v1/table/db1$users/count_rows", json={"id": ["db1", "other"]})
    assert resp.status_code == 400, resp.text
    fake_ns.count_table_rows.assert_not_called()


def test_schema_metadata_flat_map_keeps_keys_named_like_envelope_fields(client: TestClient, fake_ns: MagicMock) -> None:
    # CONTRACT (audit 2026-07-15): a FLAT body IS the metadata map — a user key literally named "id"
    # (or identity/context) is data, and must reach the backend, never be eaten as an envelope field.
    from lance_namespace import UpdateTableSchemaMetadataResponse

    fake_ns.update_table_schema_metadata.return_value = UpdateTableSchemaMetadataResponse()
    client.post("/v1/table/db1$users/schema_metadata/update", json={"id": "row-key", "owner": "alice"})
    sent = fake_ns.update_table_schema_metadata.call_args.args[0]
    assert sent.metadata == {"id": "row-key", "owner": "alice"}
    assert sent.id == ["db1", "users"]


def test_schema_metadata_envelope_with_differing_id_is_400(client: TestClient, fake_ns: MagicMock) -> None:
    resp = client.post(
        "/v1/table/db1$users/schema_metadata/update",
        json={"id": ["db1", "other"], "metadata": {"owner": "alice"}},
    )
    assert resp.status_code == 400, resp.text
    fake_ns.update_table_schema_metadata.assert_not_called()


def test_root_namespace_id_is_empty_list(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.list_namespaces.return_value = ListNamespacesResponse(namespaces=["a"])
    client.get("/v1/namespace/$/list")
    assert fake_ns.list_namespaces.call_args.args[0].id == []


def test_create_namespace_routes_with_body_and_id(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.create_namespace.return_value = CreateNamespaceResponse(properties={"team": "ml"})
    resp = client.post("/v1/namespace/parent$child/create", json={"properties": {"team": "ml"}})
    assert resp.status_code == 200
    req = fake_ns.create_namespace.call_args.args[0]
    assert req.id == ["parent", "child"]
    assert req.properties == {"team": "ml"}
    assert resp.json() == {"properties": {"team": "ml"}}  # our serialization


# --- query-param / header assembly (our logic) ----------------------------- #


def test_describe_table_maps_query_params(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://x")
    client.post("/v1/table/db$t/describe?with_table_uri=true&load_detailed_metadata=true")
    # The #74 metadata fallback (empty response.metadata + load_detailed_metadata) opens the dataset, which
    # issues a SECOND describe_table (open_dataset) without the flags — so assert on the FIRST (primary) call.
    req = fake_ns.describe_table.call_args_list[0].args[0]
    assert req.with_table_uri is True
    assert req.load_detailed_metadata is True


def _ensure_namespace(client: TestClient, name: str) -> None:
    """Create the parent namespace before creating a table under it.

    Required since pylance 9.0: the `dir` backend answers a child-namespace read with
    ``NamespaceNotFoundError: Child namespace reads require an existing __manifest dataset``, where 8.0
    treated any directory as a namespace. So a table create under an undeclared namespace is now a 404
    rather than an implicit mkdir — which is the stricter and more honest contract, and the one the
    product already follows (the namespace-create endpoint and the medallion bootstrap both declare
    theirs). These tests were relying on the implicit behaviour.
    """
    assert client.post(f"/v1/namespace/{name}/create", json={}).status_code == 200


def test_create_table_passes_arrow_bytes_through(real_ns_client: TestClient) -> None:
    _ensure_namespace(real_ns_client, "db")
    # The raw Arrow-IPC body is written verbatim as the new table's first version. Driven against a REAL
    # dir namespace (a MagicMock can no longer stand in — the create does a real 2.2 write), so this now
    # proves the ACTUAL round-trip: the rows land, not merely that bytes reached a mocked method.
    body = _arrow_ipc(pa.table({"id": [1, 2, 3]}))
    resp = real_ns_client.post("/v1/table/db$t/create?mode=overwrite", content=body, headers=ARROW_STREAM)
    assert resp.status_code == 200

    described = real_ns_client.post("/v1/table/db$t/describe", json={"load_detailed_metadata": True})
    assert described.status_code == 200
    ds = lance.dataset(described.json()["location"])
    assert ds.count_rows() == 3
    assert ds.data_storage_version == "2.2"  # the whole point of routing every create through the direct path
    assert ds.has_stable_row_ids


def test_create_table_accepts_spec_properties_query_param(real_ns_client: TestClient) -> None:
    _ensure_namespace(real_ns_client, "db")
    # Spec 0.9 passes properties as a JSON-encoded query param (no header form). The endpoint must parse it
    # and the create must succeed carrying them — driven for real so the parse + write path both run.
    body = _arrow_ipc(pa.table({"id": [1]}))
    resp = real_ns_client.post('/v1/table/db$t/create?properties={"team":"eng"}', content=body, headers=ARROW_STREAM)
    assert resp.status_code == 200


def _arrow_ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def test_create_strips_root_storage_options_from_response(real_ns_client: TestClient) -> None:
    _ensure_namespace(real_ns_client, "db")
    # #88: a create response must NEVER carry storage credentials (storage access is vended only via
    # /credentials). Driven for real: the direct 2.2 write builds its own response and never populates
    # storage_options, so the guarantee is now structural — this pins it against a regression that re-adds it.
    body = _arrow_ipc(pa.table({"id": [1]}))
    resp = real_ns_client.post("/v1/table/db$t/create", content=body, headers=ARROW_STREAM)

    assert resp.status_code == 200
    assert "storage_options" not in resp.json()
    assert "secret" not in resp.text.lower()


def test_create_delegates_to_dataplane_create_table(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # The endpoint stays thin: it delegates create; the write path lives in the facade.
    seen: dict[str, object] = {}

    def _stub(
        ns,
        so,
        segments,
        data,
        *,
        mode=None,
        properties=None,
        allow_external_blobs=False,
        external_blob_bases=None,
        data_bases=None,
    ) -> CreateTableResponse:
        seen["segments"] = segments
        seen["mode"] = mode
        seen["allow_external_blobs"] = allow_external_blobs  # proves the endpoint forwards the setting
        seen["external_blob_bases"] = external_blob_bases  # the allowlist is forwarded too
        seen["data_bases"] = data_bases  # #3-B: no ?data_base → None (backward-compatible)
        return CreateTableResponse(location="s3://x/t", version=1)

    monkeypatch.setattr("catalog.services.dataplane.create_table", _stub)
    resp = client.post(
        "/v1/table/media$clips/create?mode=overwrite",
        content=_arrow_ipc(pa.table({"id": [1]})),
        headers=ARROW_STREAM,
    )

    assert resp.status_code == 200
    assert seen == {
        "segments": ["media", "clips"],
        "mode": "overwrite",
        "allow_external_blobs": False,
        "external_blob_bases": [],
        "data_bases": None,
    }


def test_merge_insert_maps_spec_09_query_params(client: TestClient, fake_ns: MagicMock) -> None:
    """The spec-0.9 query parameters reach the native op.

    `branch` is deliberately NOT sent here any more. It used to be, and the assertion was
    `req.branch == "exp"` — i.e. it pinned the branch reaching `native.call`, which is exactly the
    defect fixed on 2026-09-01: the upstream accepted `branch` and merged into MAIN. A branch-scoped
    merge now takes the branch-aware dataplane path and never reaches this mock at all, so asserting
    the old shape would re-pin the bug. The branch behaviour is driven live instead, against the
    object store, by `tests/e2e-py/test_track_a_acceptance.py::
    test_a_branch_scoped_merge_insert_applies_to_the_branch_and_not_to_main`.
    """
    fake_ns.merge_insert_into_table.return_value = MergeInsertIntoTableResponse(version=2)
    client.post(
        "/v1/table/db$t/merge_insert?on=id&when_matched_update_all=true&when_matched_update_all_filt=score>0.5&timeout=30s&use_index=true",
        content=b"A",
        headers=ARROW_STREAM,
    )
    req = fake_ns.merge_insert_into_table.call_args.args[0]
    assert req.when_matched_update_all_filt == "score>0.5"
    assert req.timeout == "30s"
    assert req.use_index is True


def test_list_table_versions_maps_descending_and_branch(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.list_table_versions.return_value = ListTableVersionsResponse(versions=[])
    client.post("/v1/table/db$t/version/list?descending=true&branch=exp")
    req = fake_ns.list_table_versions.call_args.args[0]
    assert req.descending is True
    assert req.branch == "exp"


# --- response shaping / content types (our logic) -------------------------- #


def test_exists_returns_200(client: TestClient, fake_ns: MagicMock) -> None:
    # Spec 0.9: existence is conveyed as 200 (was 204 in earlier revisions).
    fake_ns.table_exists.return_value = None
    assert client.post("/v1/table/db$t/exists").status_code == 200


def test_count_rows_returns_a_json_integer(client: TestClient, fake_ns: MagicMock) -> None:
    """The spec's `components.responses.CountTableRowsResponse` is `application/json` holding a bare
    integer — "serialized transparently as a bare number for the REST namespace". This asserted
    `text/plain` until 2026-09-02 (blocker A2); the media type was the deviation, and the payload
    survived only because a bare number happens to parse as JSON either way."""
    fake_ns.count_table_rows.return_value = 7
    resp = client.post("/v1/table/db$t/count_rows", json={})
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == 7


def test_query_returns_arrow_file_bytes(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.query_table.return_value = b"ARROWFILEBYTES"
    resp = client.post("/v1/table/db$t/query", json={"k": 5, "vector": {}})
    assert resp.headers["content-type"].startswith("application/vnd.apache.arrow.file")
    assert resp.content == b"ARROWFILEBYTES"


# --- error → HTTP / Problem-Details mapping (our logic) --------------------- #


def test_domain_not_found_maps_to_404_problem_json(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table.side_effect = TableNotFoundError("table 'x' not found")
    resp = client.post("/v1/table/db$t/describe")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 404 and body["code"] == 4


def test_domain_conflict_maps_to_409(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.declare_table.side_effect = TableAlreadyExistsError("exists")
    assert client.post("/v1/table/db$t/declare", json={}).status_code == 409


def test_backend_stub_message_maps_to_501(client: TestClient, fake_ns: MagicMock) -> None:
    # native.call laundering: a backend that stubs an op with "not implemented" surfaces as 501 (spec
    # "unsupported"), not a 500. rename_table is now implemented in-process (#5b), so register_table — a
    # still-native-delegated op — stands in for a genuinely-unwired backend op.
    fake_ns.register_table.side_effect = RuntimeError("register_table not implemented")
    resp = client.post("/v1/table/db$t/register", json={"location": "s3://b/db$t"})
    assert resp.status_code == 501
    assert resp.json()["status"] == 501


def test_list_branches_routes_to_dataset_branches(client: TestClient, monkeypatch) -> None:
    # Branches are now backed in-process via pylance `ds.branches` (was a native 501).
    dataset = MagicMock()
    dataset.branches.list.return_value = {"exp": {"parent_branch": None, "parent_version": 2, "create_at": 1, "manifest_size": 9}}
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    resp = client.post("/v1/table/db1$users/branches/list")
    assert resp.status_code == 200
    assert resp.json()["branches"]["exp"]["parentVersion"] == 2  # serialized with the spec's camelCase alias


def test_create_branch_maps_from_version_to_int_reference(client: TestClient, monkeypatch) -> None:
    dataset = MagicMock()
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    resp = client.post("/v1/table/db$t/branches/create", json={"name": "exp", "from_version": 3})
    assert resp.status_code == 200
    dataset.create_branch.assert_called_once_with("exp", 3)  # fromVersion → int reference


def test_create_branch_maps_from_branch_and_version_to_tuple(client: TestClient, monkeypatch) -> None:
    dataset = MagicMock()
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    resp = client.post("/v1/table/db$t/branches/create", json={"name": "x", "from_branch": "exp", "from_version": 2})
    assert resp.status_code == 200
    dataset.create_branch.assert_called_once_with("x", ("exp", 2))  # (branch, version) reference


def test_create_branch_from_main_uses_no_reference(client: TestClient, monkeypatch) -> None:
    dataset = MagicMock()
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    resp = client.post("/v1/table/db$t/branches/create", json={"name": "exp"})
    assert resp.status_code == 200
    dataset.create_branch.assert_called_once_with("exp", None)  # neither → latest of main


def test_delete_branch_routes_to_dataset(client: TestClient, monkeypatch) -> None:
    dataset = MagicMock()
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    resp = client.post("/v1/table/db$t/branches/delete", json={"name": "exp"})
    assert resp.status_code == 200
    dataset.branches.delete.assert_called_once_with("exp")


def test_insert_stamps_the_real_version_on_lineage(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # Insert's native response carries only a transaction_id; the shared trailer reopens the dataset for
    # the version it produced and stamps it on the WROTE edge — it used to emit version=None.
    from lance_namespace import InsertIntoTableResponse

    fake_ns.insert_into_table.return_value = InsertIntoTableResponse(transaction_id="tx1")
    dataset = MagicMock()
    dataset.version = 7
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    # Schema coercion (real Arrow parse + live-schema open) is orthogonal to lineage version-stamping and
    # has its own coverage (test_insert_coerce.py); pass the placeholder bytes through to the mocked native.
    monkeypatch.setattr("catalog.services.dataplane.coerce_insert_arrow", lambda _ns, _so, _seg, data: data)
    captured = _capture_measured_emit(monkeypatch)

    resp = client.post("/v1/table/db$t/insert", content=b"ARROWSTREAM", headers=ARROW_STREAM)
    assert resp.status_code == 200
    assert captured["version"] == 7  # the real Lance version, not None


# --- #110 lineage-emit coverage: schema-evolution / index / restore / register / declare now emit, ---
# --- through the shared best-effort + version-pinned read-back trailer (lineage_deps).            ---


def _capture_emit(monkeypatch, module: str) -> dict[str, object]:
    """Patch the endpoint module's emit_write_event to record its kwargs (versionless-marker ops)."""
    captured: dict[str, object] = {}

    async def _cap(_emitter: object, _segments: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(f"catalog.api.v1.endpoints.{module}.emit_write_event", _cap)
    return captured


def _capture_measured_emit(monkeypatch) -> dict[str, object]:
    """Patch the shared trailer's emit_write_event to record what a measured op finally emits."""
    captured: dict[str, object] = {}

    async def _cap(_emitter: object, _segments: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("catalog.api.lineage_deps.emit_write_event", _cap)
    return captured


def test_add_columns_emits_pinned_schema_evolution_lineage(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # A schema change must carry the NEW per-version schema so /schema + /columns follow the evolution —
    # and the read-back must be PINNED to the response's version, so a concurrent writer between the
    # commit and the read can't attach a later version's schema to this WROTE edge.
    from lance_namespace import AlterTableAddColumnsResponse

    monkeypatch.setattr("catalog.services.dataplane.add_columns", lambda *a, **k: AlterTableAddColumnsResponse(version=4))
    seen: dict[str, object] = {}

    def _readback(_ns: object, _so: object, _segments: object, pin_version: object = None, branch: object = None) -> object:
        seen["pin"] = pin_version
        seen["branch"] = branch
        return pin_version, [{"name": "x", "type": "int64"}]

    monkeypatch.setattr("catalog.services.dataplane.read_version_and_schema", _readback)
    captured = _capture_measured_emit(monkeypatch)

    resp = client.post("/v1/table/db$t/add_columns", json={"new_columns": [{"name": "x", "expression": "1"}]})
    assert resp.status_code == 200
    assert seen["pin"] == 4  # the read-back opened the dataset AT the version the response reported
    assert seen["branch"] is None  # no branch on the request → the read-back stays on main
    assert captured["operation"] == "add_columns"
    assert captured["version"] == 4
    assert captured["schema_fields"] == [{"name": "x", "type": "int64"}]  # post-evolution schema rides along


def test_merge_insert_emits_version_pinned_source_and_run_facets(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # Phase 2: a mover's merge from source@N. The `source` + `source_version` query params and the
    # `X-Lance-Run-Facets` header must reach the emit trailer as a version-pinned InputPin + spec-shaped
    # run facets — training-shaped OpenLineage, with the catalog un-opinionated about the facet payload.
    from catalog.core.lineage_emit import InputPin, shape_run_facets

    fake_ns.merge_insert_into_table.return_value = MergeInsertIntoTableResponse(version=2)
    monkeypatch.setattr(
        "catalog.services.dataplane.read_version_and_schema",
        lambda *a, **k: (2, [{"name": "id", "type": "int64"}]),
    )
    # The implicit BTREE build opens the real dataset — orthogonal to lineage; stub it out.
    monkeypatch.setattr("catalog.services.dataplane.ensure_merge_key_index", lambda *a, **k: None)
    captured = _capture_measured_emit(monkeypatch)

    resp = client.post(
        "/v1/table/db$t/merge_insert?on=id&when_matched_update_all=true&source=db$src&source_version=4",
        content=b"A",
        headers={**ARROW_STREAM, "X-Lance-Run-Facets": '{"params": {"lr": 0.01, "epochs": 5}}'},
    )
    assert resp.status_code == 200
    assert captured["operation"] == "merge_insert"
    assert captured["inputs"] == [InputPin(segments=["db", "src"], version=4)]  # the version-pinned source
    # The header payload is carried verbatim onto the run, stamped spec-legal by the catalog (custom_facet
    # _producer/_schemaURL) — exactly what shape_run_facets produces from the same JSON.
    assert captured["extra_run_facets"] == shape_run_facets({"params": {"lr": 0.01, "epochs": 5}})


def test_merge_insert_rejects_source_version_without_source(client: TestClient, fake_ns: MagicMock) -> None:
    # A pin with nothing to pin is a fail-fast 400 (before the merge commits), not a silently dropped version.
    resp = client.post("/v1/table/db$t/merge_insert?on=id&source_version=4", content=b"A", headers=ARROW_STREAM)
    assert resp.status_code == 400, resp.text


def test_merge_insert_authz_checks_the_source_before_recording_it(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # The named source must be READ-authorized by the caller (mirrors the lineage ingest input guard) so it
    # can't forge a cross-tenant DERIVED_FROM/READ edge on the trusted Dapr transport. Assert the handler runs
    # require_can_get_metadata against the SOURCE's segments (not the merged table's).
    from catalog.api import fga_deps

    seen: dict[str, object] = {}

    async def _cap(_client, _settings, _token, *, segments):
        seen["segments"] = segments

    monkeypatch.setattr(fga_deps, "require_can_get_metadata", _cap)
    monkeypatch.setattr("catalog.services.dataplane.read_version_and_schema", lambda *a, **k: (2, []))
    monkeypatch.setattr("catalog.services.dataplane.ensure_merge_key_index", lambda *a, **k: None)
    fake_ns.merge_insert_into_table.return_value = MergeInsertIntoTableResponse(version=2)

    resp = client.post(
        "/v1/table/db$t/merge_insert?on=id&source=up$stream&source_version=3",
        content=b"A",
        headers=ARROW_STREAM,
    )
    assert resp.status_code == 200
    assert seen["segments"] == ["up", "stream"]  # the SOURCE, not the merged table db$t


def test_merge_insert_denies_when_source_not_readable(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # Fail-closed: a source the caller can't read is a 403 BEFORE the merge — never a silent forged edge.
    from lance_namespace import PermissionDeniedError

    from catalog.api import fga_deps

    async def _deny(*_a, **_k):
        raise PermissionDeniedError("can_get_metadata required")

    monkeypatch.setattr(fga_deps, "require_can_get_metadata", _deny)
    resp = client.post(
        "/v1/table/db$t/merge_insert?on=id&source=secret$pii&source_version=1",
        content=b"A",
        headers=ARROW_STREAM,
    )
    assert resp.status_code == 403, resp.text
    fake_ns.merge_insert_into_table.assert_not_called()  # denied before the write


def test_merge_insert_rejects_malformed_or_reserved_run_facets(client: TestClient, fake_ns: MagicMock) -> None:
    # A malformed / reserved-name / reserved-key X-Lance-Run-Facets header is a fail-fast 400 (never a 500),
    # before the merge — the run-facet forgery + 500-from-header findings. The last two are the re-audit
    # catch: json.loads raises a bare ValueError past the 4300-digit int limit, and RecursionError on deep
    # nesting; neither is a JSONDecodeError, so the original narrow except missed them (→ 500).
    bad_headers = [
        "{not json",
        '"a string"',
        '{"author": {"sub": "admin"}}',
        '{"params": {"producer": "x"}}',
        '{"params": {"n": ' + "9" * 4400 + "}}",  # >4300-digit int → bare ValueError, not JSONDecodeError
        "[" * 60000 + "]" * 60000,  # deep nesting → RecursionError
    ]
    for bad in bad_headers:
        resp = client.post(
            "/v1/table/db$t/merge_insert?on=id",
            content=b"A",
            headers={**ARROW_STREAM, "X-Lance-Run-Facets": bad},
        )
        assert resp.status_code == 400, f"{bad[:40]!r} -> {resp.status_code}: {resp.text[:200]}"
    fake_ns.merge_insert_into_table.assert_not_called()


def test_merge_insert_rejects_blank_source_with_version(client: TestClient, fake_ns: MagicMock) -> None:
    # An empty source can't carry a version pin — 400, not a nameless forged input vertex bypassing the guard.
    resp = client.post("/v1/table/db$t/merge_insert?on=id&source=&source_version=4", content=b"A", headers=ARROW_STREAM)
    assert resp.status_code == 400, resp.text
    fake_ns.merge_insert_into_table.assert_not_called()
    fake_ns.merge_insert_into_table.assert_not_called()  # rejected before the write


def test_create_index_emits_lineage_at_readback_version(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # The native index response carries only a transaction_id → the new manifest version is read back.
    from lance_namespace import CreateTableIndexResponse

    fake_ns.create_table_index.return_value = CreateTableIndexResponse(transaction_id="tx")
    dataset = MagicMock()
    dataset.version = 9
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    captured = _capture_measured_emit(monkeypatch)

    resp = client.post("/v1/table/db$t/create_index", json={"column": "vec", "index_type": "IVF_PQ"})
    assert resp.status_code == 200
    assert captured["operation"] == "create_index"
    assert captured["version"] == 9  # read back off the dataset, not None


def test_create_index_succeeds_when_readback_fails(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # The index op is COMMITTED before the lineage read-back runs — a transient reopen failure must
    # degrade the emit to versionless, never turn the committed op into an error response.
    from lance_namespace import CreateTableIndexResponse

    fake_ns.create_table_index.return_value = CreateTableIndexResponse(transaction_id="tx")

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("transient object-store hiccup")

    monkeypatch.setattr("catalog.services.dataplane.open_dataset", _boom)
    captured = _capture_measured_emit(monkeypatch)

    resp = client.post("/v1/table/db$t/create_index", json={"column": "vec", "index_type": "IVF_PQ"})
    assert resp.status_code == 200  # the committed index build still returns success
    assert captured["operation"] == "create_index"
    assert captured["version"] is None  # degraded to a versionless marker; reconcile recovers the version
    assert captured["schema_fields"] == []


def test_restore_emits_lineage_at_new_version(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    from lance_namespace import RestoreTableResponse

    fake_ns.restore_table.return_value = RestoreTableResponse(transaction_id="tx")
    dataset = MagicMock()
    dataset.version = 12
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    captured = _capture_measured_emit(monkeypatch)

    resp = client.post("/v1/table/db$t/restore", json={"version": 3})
    assert resp.status_code == 200
    assert captured["operation"] == "restore_table"
    assert captured["version"] == 12  # the NEW current version after restore


def test_create_exist_ok_reads_schema_back_instead_of_trusting_payload(real_ns_client: TestClient, monkeypatch) -> None:
    _ensure_namespace(real_ns_client, "db")
    # ExistOk KEEPS an existing table (nothing written, response.version = the existing version): the
    # payload's schema may then belong to a table that was never created — the true schema must be read back
    # PINNED at that version, never parsed from the payload. Now driven for real: the table genuinely exists.
    body = _arrow_ipc(pa.table({"id": [1]}))
    assert real_ns_client.post("/v1/table/db$t/create", content=body, headers=ARROW_STREAM).status_code == 200

    seen: dict[str, object] = {}

    def _readback(_ns: object, _so: object, _segments: object, pin_version: object = None) -> object:
        seen["pin"] = pin_version
        return pin_version, [{"name": "true_col", "type": "int64"}]

    def _payload_bomb(*_a: object, **_k: object) -> object:
        raise AssertionError("ExistOk create must not trust the request payload's schema")

    # Patch the enrichment functions only AFTER the setup create, so the bomb guards the exist_ok call alone.
    monkeypatch.setattr("catalog.services.dataplane.read_version_and_schema", _readback)
    monkeypatch.setattr("catalog.services.dataplane.payload_schema_fields", _payload_bomb)

    resp = real_ns_client.post("/v1/table/db$t/create?mode=exist_ok", content=body, headers=ARROW_STREAM)
    assert resp.status_code == 200
    assert "pin" in seen  # the readback path was taken (not the payload path)
    assert seen["pin"] == 1  # schema read back pinned at the EXISTING (freshly-created) table's version


def test_create_parses_payload_schema_without_dataset_reopen(real_ns_client: TestClient, monkeypatch) -> None:
    _ensure_namespace(real_ns_client, "db")
    # A fresh create writes exactly the request bytes — the schema facet comes from the in-memory payload,
    # never from a describe + dataset reopen (which would add two network round trips per create).
    called: dict[str, object] = {}

    def _payload(_data: object, _segments: object) -> object:
        called["payload"] = True
        return [{"name": "p", "type": "int64"}]

    def _readback_bomb(*_a: object, **_k: object) -> object:
        raise AssertionError("plain create must not reopen the dataset for its schema")

    monkeypatch.setattr("catalog.services.dataplane.payload_schema_fields", _payload)
    monkeypatch.setattr("catalog.services.dataplane.read_version_and_schema", _readback_bomb)

    body = _arrow_ipc(pa.table({"id": [1]}))
    resp = real_ns_client.post("/v1/table/db$t/create", content=body, headers=ARROW_STREAM)
    assert resp.status_code == 200
    assert called.get("payload") is True


def test_deregister_emits_marker_before_revoking_tuples(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # The DEREGISTER_TABLE marker must publish BEFORE revoke_ownership: on the http transport the caller's
    # bearer authorizes ingest against their write grant — revoke-first would 403 the marker (silently
    # dropped by the best-effort emitter) and the graph would keep showing the table as live.
    from lance_namespace import DeregisterTableResponse

    fake_ns.deregister_table.return_value = DeregisterTableResponse()
    order: list[str] = []

    async def _emit(*_a: object, **_k: object) -> None:
        order.append("emit")

    async def _revoke(*_a: object, **_k: object) -> None:
        order.append("revoke")

    monkeypatch.setattr("catalog.api.v1.endpoints.tables.emit_write_event", _emit)
    monkeypatch.setattr("catalog.api.fga_deps.revoke_ownership", _revoke)

    resp = client.post("/v1/table/db$t/deregister")
    assert resp.status_code == 200
    assert order == ["emit", "revoke"]  # marker first, while the caller's grant still authorizes ingest


def test_register_emits_versionless_marker_with_source_uri(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    # Register attaches an existing (possibly external) location: versionless + source_uri, and it keys a
    # CREATED edge (register_table ∈ lineage _CREATE_OPS); reconcile back-fills the real on-disk version.
    from lance_namespace import RegisterTableResponse

    fake_ns.register_table.return_value = RegisterTableResponse(location="s3://bucket/t")
    captured = _capture_emit(monkeypatch, "tables")

    resp = client.post("/v1/table/db$t/register", json={"location": "s3://bucket/t"})
    assert resp.status_code == 200
    assert captured["operation"] == "register_table"
    assert captured["version"] is None  # versionless — no reopen of a possibly-external location on the path
    assert captured["source_uri"] == "s3://bucket/t"


def test_declare_emits_versionless_marker(client: TestClient, fake_ns: MagicMock, monkeypatch) -> None:
    from lance_namespace import DeclareTableResponse

    fake_ns.declare_table.return_value = DeclareTableResponse(location="s3://bucket/t")
    captured = _capture_emit(monkeypatch, "tables")

    resp = client.post("/v1/table/db$t/declare", json={})
    assert resp.status_code == 200
    assert captured["operation"] == "declare_table"
    assert captured["version"] is None  # reserved, no data yet
    assert captured["source_uri"] == "s3://bucket/t"


# --- version ops: the native bindings are `request: dict`-typed; native.call must marshal the pydantic ---
# --- request to a dict, else a TypeError surfaces as a fake 501. These guard that fix (audit finding). ---


def test_describe_version_marshals_request_to_a_dict(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table_version.return_value = DescribeTableVersionResponse(version=TableVersion(version=2, manifest_path="_versions/2.manifest"))
    resp = client.post("/v1/table/db$t/version/describe?version=2")
    assert resp.status_code == 200
    assert resp.json()["version"]["version"] == 2
    arg = fake_ns.describe_table_version.call_args.args[0]
    assert isinstance(arg, dict) and arg["id"] == ["db", "t"] and arg["version"] == 2  # a dict, not the model


def test_describe_version_missing_maps_to_404(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_table_version.side_effect = TableVersionNotFoundError("version 99 not found")
    resp = client.post("/v1/table/db$t/version/describe?version=99")
    assert resp.status_code == 404


def test_create_version_marshals_request_to_a_dict(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.create_table_version.return_value = CreateTableVersionResponse()
    resp = client.post("/v1/table/db$t/version/create", json={"version": 2, "manifest_path": "_versions/2.manifest"})
    assert resp.status_code == 200
    arg = fake_ns.create_table_version.call_args.args[0]
    assert isinstance(arg, dict) and arg["id"] == ["db", "t"] and arg["version"] == 2


def test_batch_delete_versions_marshals_request_to_a_dict(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.batch_delete_table_versions.return_value = BatchDeleteTableVersionsResponse()
    resp = client.post("/v1/table/db$t/version/delete", json={"ranges": [{"start_version": 1, "end_version": 1}]})
    assert resp.status_code == 200
    arg = fake_ns.batch_delete_table_versions.call_args.args[0]
    assert isinstance(arg, dict) and arg["id"] == ["db", "t"]


def test_request_validation_maps_to_422_problem_json(client: TestClient) -> None:
    # UpdateTableRequest requires `updates`; an empty body fails validation.
    resp = client.post("/v1/table/db$t/update", json={})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["title"] == "Validation Error"


# --- data-plane request building (our logic; fake dataset) ----------------- #


def test_update_builds_updates_dict_and_reads_real_count_key(client: TestClient, monkeypatch) -> None:
    dataset = MagicMock()
    # pylance's update() returns the UpdateResult dict with key `num_rows_updated` (NOT `num_updated_rows`,
    # and NOT an attribute) — the response must read that exact key, else updated_rows is always 0.
    dataset.update.return_value = {"num_rows_updated": 2}
    dataset.version = 5
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)

    resp = client.post("/v1/table/db$t/update", json={"predicate": "id = 1", "updates": [["name", "'x'"]]})
    assert resp.status_code == 200
    assert dataset.update.call_args.args[0] == {"name": "'x'"}  # list-of-pairs -> dict
    assert dataset.update.call_args.kwargs["where"] == "id = 1"
    assert resp.json() == {"updated_rows": 2, "version": 5}


def test_add_columns_builds_transforms(client: TestClient, monkeypatch) -> None:
    dataset = MagicMock()
    dataset.version = 3
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)

    resp = client.post(
        "/v1/table/db$t/add_columns",
        json={"new_columns": [{"name": "score", "expression": "cast(id as double)"}]},
    )
    assert resp.status_code == 200
    assert dataset.add_columns.call_args.args[0] == {"score": "cast(id as double)"}


def test_create_tag_routes_to_dataset_tags(client: TestClient, monkeypatch) -> None:
    dataset = MagicMock()
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)

    resp = client.post("/v1/table/db$t/tags/create", json={"tag": "v1", "version": 1})
    assert resp.status_code == 200
    dataset.tags.create.assert_called_once_with("v1", 1)  # no branch → bare int (current/main branch)


def test_create_tag_with_branch_passes_branch_version_tuple(client: TestClient, monkeypatch) -> None:
    # A branch-scoped tag must pass (branch, version) — a bare int would resolve against main, tagging the
    # WRONG version. (Was silently dropping `branch`.)
    dataset = MagicMock()
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    resp = client.post("/v1/table/db$t/tags/create", json={"tag": "rc", "version": 5, "branch": "dev"})
    assert resp.status_code == 200
    dataset.tags.create.assert_called_once_with("rc", ("dev", 5))


def test_alter_columns_converts_json_arrow_type_to_pyarrow(client: TestClient, monkeypatch) -> None:
    # A re-type carries `data_type` as a JsonArrowDataType dict; it must reach pylance as a real pa.DataType,
    # not the JSON dict (which would 500 at the Rust boundary). float32→float16 is the documented vector case.
    import pyarrow as pa

    dataset = MagicMock()
    dataset.version = 4
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: dataset)
    resp = client.post(
        "/v1/table/db$t/alter_columns",
        json={"alterations": [{"path": "embedding", "data_type": {"type": "float16"}}]},
    )
    assert resp.status_code == 200
    alteration = dataset.alter_columns.call_args.args[0]  # alterations are passed positionally (*alterations)
    assert alteration["data_type"] == pa.float16()


def test_alter_columns_unsupported_type_is_400_not_500(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: MagicMock())
    resp = client.post(
        "/v1/table/db$t/alter_columns",
        json={"alterations": [{"path": "c", "data_type": {"type": "some_exotic_type"}}]},
    )
    assert resp.status_code == 400  # clear InvalidInput, not a silent Rust-boundary 500


def test_update_with_empty_updates_is_400(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("catalog.services.dataplane.open_dataset", lambda *a, **k: MagicMock())
    resp = client.post("/v1/table/db$t/update", json={"updates": []})
    assert resp.status_code == 400
    assert resp.json()["code"] == 13  # InvalidInput


def test_maintenance_policy_crud_round_trips(client: TestClient, fake_ns: MagicMock) -> None:
    # CONTRACT (#50): set resolves the physical path from describe_table and persists it; describe
    # returns the record; delete is idempotent and describe 404s afterwards.
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://lance-test-root/u1_db1$users")
    set_resp = client.post("/v1/table/db1$users/policy/set", json={"retention_days": 7, "compact_interval_hours": 12})
    assert set_resp.status_code == 200, set_resp.text
    body = set_resp.json()
    assert body["path"] == "lance-test-root/u1_db1$users" and body["retention_days"] == 7

    desc = client.post("/v1/table/db1$users/policy/describe")
    assert desc.status_code == 200 and desc.json()["compact_interval_hours"] == 12

    assert client.post("/v1/table/db1$users/policy/delete").status_code == 200
    assert client.post("/v1/table/db1$users/policy/describe").status_code == 404


def _project_policy_settings(tmp_path: object):
    from catalog.core.config import Settings

    # A LOCAL control root so the warehouse registry + policy records round-trip on the FS per test
    # (the shared fixture's fixed /tmp root would leak records across tests).
    return Settings.model_validate(
        {
            "impl": "dir",
            "root": "s3://lance-catalog",
            "control_root": f"file://{tmp_path}",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


def test_project_policy_crud_round_trips(client: TestClient, tmp_path: object) -> None:
    # CONTRACT (#84): set resolves the project's ACTIVE warehouse buckets from the registry at set time
    # and persists them on the record; describe returns it; delete is idempotent, then describe 404s.
    from catalog.core.config import get_settings
    from catalog.services import warehouses as wh_svc

    s = _project_policy_settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    so = s.storage_options()
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-b", "bucket": "acme-old", "project": "acme", "status": "deactivated"})
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-c", "bucket": "other-wh", "project": "other", "status": "active"})

    set_resp = client.post("/v1/project/acme/policy/set", json={"retention_days": 90})
    assert set_resp.status_code == 200, set_resp.text
    body = set_resp.json()
    # Only the project's own ACTIVE bucket is covered — never a deactivated one or another tenant's.
    assert body["kind"] == "project" and body["buckets"] == ["acme-wh"] and body["retention_days"] == 90

    desc = client.post("/v1/project/acme/policy/describe")
    assert desc.status_code == 200 and desc.json()["buckets"] == ["acme-wh"]

    assert client.post("/v1/project/acme/policy/delete").status_code == 200
    assert client.post("/v1/project/acme/policy/describe").status_code == 404


def test_project_policy_set_refused_without_an_active_warehouse(client: TestClient, tmp_path: object) -> None:
    # A policy that could never match anything must fail loudly at set time, not lie dormant.
    from catalog.core.config import get_settings

    client.app.dependency_overrides[get_settings] = lambda: _project_policy_settings(tmp_path)
    resp = client.post("/v1/project/ghost/policy/set", json={"retention_days": 90})
    assert resp.status_code == 400
    assert "no active warehouse" in resp.json()["error"]


def test_project_policy_set_refused_on_a_cross_claimed_bucket(client: TestClient, tmp_path: object) -> None:
    # Mallory layer 2 (audit 2026-07-23, defense in depth): even if a rival bucket claim got PAST the
    # create_warehouse guards (records written straight into the registry here), a policy set that would
    # resolve a bucket another project's warehouse also claims must be refused — never a retention policy
    # over the other tenant's data.
    from catalog.core.config import get_settings
    from catalog.services import warehouses as wh_svc

    s = _project_policy_settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    so = s.storage_options()
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "shared-bkt", "project": "acme", "status": "active"})
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-evil", "bucket": "shared-bkt", "project": "evil", "status": "active"})
    resp = client.post("/v1/project/evil/policy/set", json={"retention_days": 1, "retain_versions": 1})
    assert resp.status_code == 409, resp.text


def test_project_policy_set_tolerates_a_corrupt_registry_record(client: TestClient, tmp_path: object) -> None:
    # A corrupt warehouse record next to the project's own must not 500 the set (skip-with-warning): the
    # policy still resolves the readable active bucket.
    from pathlib import Path

    from catalog.core.config import get_settings
    from catalog.services import warehouses as wh_svc

    s = _project_policy_settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    wh_svc.put_warehouse(
        s.registry_root,
        s.storage_options(),
        {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"},
    )
    (Path(str(tmp_path)) / "_warehouses" / "zzz-corrupt.json").write_text("{truncated")
    resp = client.post("/v1/project/acme/policy/set", json={"retention_days": 90})
    assert resp.status_code == 200, resp.text
    assert resp.json()["buckets"] == ["acme-wh"]
    assert client.post("/v1/project/acme/policy/delete").status_code == 200  # no leak across tests


def test_project_policy_rejects_a_malformed_project_id(client: TestClient, tmp_path: object) -> None:
    from catalog.core.config import get_settings

    client.app.dependency_overrides[get_settings] = lambda: _project_policy_settings(tmp_path)
    resp = client.post("/v1/project/Bad_Name/policy/set", json={"retention_days": 90})
    assert resp.status_code == 400


# --- GET /v1/projects/{id}/policies — the #65 project-scoped VIEW -------------------------- #
#
# Real stores throughout (a local-FS control root through pyarrow.fs): real warehouse records, real
# namespace bindings, real policy records written by the same `put_policy` the catalog uses. The one
# thing these must prove is that scoping is BINDING-derived and not bucket-derived, because
# `set_namespace_policy` stores the DEFAULT root on every record (see the policies.py module docstring).


def _list_policies_for(client: TestClient, project: str) -> dict[str, Any]:
    """The listing, asserted 200 — the settings override is already in place at every call site."""
    resp = client.get(f"/v1/projects/{project}/policies")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict)
    return body


def test_project_policies_list_scopes_to_the_projects_own_buckets(client: TestClient, tmp_path: object) -> None:
    # CONTRACT (#65): a tenant's policy view carries the tenant's records and NOTHING else. Two
    # warehouses, two table policies, one bucket each — the rival's record must never cross.
    from catalog.core.config import get_settings
    from catalog.services import warehouses as wh_svc
    from service_kit.lakehouse import maintenance_policies as pol_svc

    s = _project_policy_settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    so = s.storage_options()
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-z", "bucket": "other-wh", "project": "other", "status": "active"})
    pol_svc.put_policy(s.registry_root, so, {"kind": "table", "id": "db$t", "path": "acme-wh/u1_db$t", "retention_days": 7})
    pol_svc.put_policy(s.registry_root, so, {"kind": "table", "id": "rival$t", "path": "other-wh/u2_rival$t", "retention_days": 1})

    body = _list_policies_for(client, "acme")
    assert [p["id"] for p in body["policies"]] == ["db$t"]
    assert body["buckets"] == ["acme-wh"] and body["incomplete"] is False


def test_project_policies_list_finds_a_warehouse_bound_namespace_policy(client: TestClient, tmp_path: object) -> None:
    # THE decisive one. `set_namespace_policy` builds its path from the DEFAULT root (it takes no
    # NamespaceDep, so it never resolves the namespace's warehouse root) — so a bound namespace's record
    # carries `lance-catalog/gold` even though its data lives in `acme-wh`. Scoping by the record's bucket
    # would miss it AND hand it to whoever owns the default bucket; scoping by the BINDING finds it.
    from catalog.core.config import get_settings
    from catalog.services import warehouses as wh_svc
    from service_kit.lakehouse import maintenance_policies as pol_svc

    s = _project_policy_settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    so = s.storage_options()
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    wh_svc.bind_namespace(s.registry_root, so, "gold", "wh-a", "s3://acme-wh")
    # Byte-for-byte what set_namespace_policy writes: the id is logical, the path is the DEFAULT root.
    pol_svc.put_policy(s.registry_root, so, {"kind": "namespace", "id": "gold", "path": "lance-catalog/gold", "retention_days": 30})

    body = _list_policies_for(client, "acme")
    assert [p["id"] for p in body["policies"]] == ["gold"]
    assert body["namespaces"] == ["gold"]  # the scope the listing was computed from, reported


def test_project_policies_list_includes_a_deactivated_warehouses_bucket(client: TestClient, tmp_path: object) -> None:
    # Reading coverage is not writing it: `set_project_policy` filters to ACTIVE warehouses because a
    # policy over a dead bucket could never match, but a deactivated warehouse's data — and any policy
    # still governing it — is still the tenant's. Narrowing here would hide the retention record an
    # operator is looking for at exactly the moment they are offboarding.
    from catalog.core.config import get_settings
    from catalog.services import warehouses as wh_svc
    from service_kit.lakehouse import maintenance_policies as pol_svc

    s = _project_policy_settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    so = s.storage_options()
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-old", "bucket": "acme-old", "project": "acme", "status": "deactivated"})
    pol_svc.put_policy(s.registry_root, so, {"kind": "table", "id": "arch$t", "path": "acme-old/u9_arch$t", "compact_enabled": False})

    body = _list_policies_for(client, "acme")
    assert [p["id"] for p in body["policies"]] == ["arch$t"]
    assert body["buckets"] == ["acme-old"]


def test_project_policies_list_returns_only_its_own_project_record(client: TestClient, tmp_path: object) -> None:
    # A project record matches by ID and by nothing else. The rival record here claims acme's own bucket —
    # the contested-registry state `set_project_policy` refuses at write time — so a bucket match would
    # surface another tenant's project policy inside acme's view.
    from catalog.core.config import get_settings
    from catalog.services import warehouses as wh_svc
    from service_kit.lakehouse import maintenance_policies as pol_svc

    s = _project_policy_settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    so = s.storage_options()
    wh_svc.put_warehouse(s.registry_root, so, {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    pol_svc.put_policy(s.registry_root, so, {"kind": "project", "id": "acme", "path": "", "buckets": ["acme-wh"], "retention_days": 90})
    pol_svc.put_policy(s.registry_root, so, {"kind": "project", "id": "other", "path": "", "buckets": ["acme-wh"], "retention_days": 1})

    body = _list_policies_for(client, "acme")
    assert [(p["kind"], p["id"]) for p in body["policies"]] == [("project", "acme")]


def test_project_policies_list_reports_an_unreadable_binding_instead_of_narrowing(client: TestClient, tmp_path: object) -> None:
    # A binding you cannot read is a namespace you cannot see. The listing still answers 200 (it is a read,
    # not the destructive door `namespaces_bound_to` guards) but SAYS it may be short — a silently
    # narrowed list would read as checked-and-clean, the `unbound_namespaces` bug class.
    from pathlib import Path

    from catalog.core.config import get_settings
    from catalog.services import warehouses as wh_svc

    s = _project_policy_settings(tmp_path)
    client.app.dependency_overrides[get_settings] = lambda: s
    wh_svc.put_warehouse(s.registry_root, s.storage_options(), {"id": "wh-a", "bucket": "acme-wh", "project": "acme", "status": "active"})
    bindings = Path(str(tmp_path)) / "_warehouses" / "bindings"
    bindings.mkdir(parents=True, exist_ok=True)
    (bindings / "zzz.json").write_text("{truncated")

    body = _list_policies_for(client, "acme")
    assert body["incomplete"] is True
    assert [str(p).endswith("zzz.json") for p in body["skipped_bindings"]] == [True]


# The listing's own 403 lives with the other three project-policy gates, in `test_authz.py`
# (`test_project_policies_list_requires_project_admin`) — one file owns the authz contract, and a
# fourth door asserted somewhere else is how a tier splits without anyone noticing.


def test_access_list_is_unsupported_without_fga(client: TestClient) -> None:
    # CONTRACT (#51): an auth-off stack has no grants to review — answer 501 honestly instead of an
    # empty grant list that would read as "nobody has access".
    resp = client.post("/v1/table/db1$users/access/list")
    # The problem handler masks 5xx details, so only the status is visible to the client.
    assert resp.status_code == 501


def test_maintenance_policy_rejects_an_empty_policy(client: TestClient, fake_ns: MagicMock) -> None:
    resp = client.post("/v1/table/db1$users/policy/set", json={})
    assert resp.status_code == 422  # a body that sets nothing changes nothing — the validator refuses
    fake_ns.describe_table.assert_not_called()
    # But an EXPLICIT compact_enabled=true is meaningful (a table-level re-enable under a disabled
    # namespace policy — the exact-table match shadows the namespace record), so it must be accepted.
    fake_ns.describe_table.return_value = DescribeTableResponse(location="s3://lance-test-root/u1_db1$users")
    resp = client.post("/v1/table/db1$users/policy/set", json={"compact_enabled": True})
    assert resp.status_code == 200, resp.text
    # The client fixture's registry root is a fixed local path — delete so no record leaks across tests.
    assert client.post("/v1/table/db1$users/policy/delete").status_code == 200
