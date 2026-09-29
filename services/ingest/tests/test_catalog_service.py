"""The catalog SERVICE client — the swap that lets a commit be seen by anything but this process.

`LocalCatalog` records versions in a Python list. In a cluster that means the run lands its data and
nothing downstream learns of it: the event that wakes a stage runner is the CATALOG's publication of a new
version, so a locally-recorded commit is a commit the cascade cannot ride.

These pin the wire contract against `respx`, which intercepts at the httpx transport rather than by
patching our own functions — so the assertions are about the REQUEST that would reach the catalog,
not about whether a mock was called. The live doors were confirmed on the deployed pod's own
openapi.json: `/v1/table/{id}/describe`, `/create` and `/commit` all exist.
"""

from __future__ import annotations

import json

import httpx
import pyarrow as pa
import pytest
import respx

from ingest.catalog_service import CatalogError, CatalogServiceClient, build_catalog


BRONZE = pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), pa.field("payload", pa.binary())])
BASE = "http://catalog.test"


def _client() -> CatalogServiceClient:
    return CatalogServiceClient(BRONZE, base_url=BASE)


@pytest.fixture(autouse=True)
def _contract_check_is_covered_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file is about the catalog PROTOCOL — which call, which body, which status means what.

    `ensure` also asserts the A14 creation contract on every return, and against the `s3://` locations
    these tests hand back that means vending a credential and OPENING the dataset. Neither exists here:
    the URIs are fixtures, so the open fails with `Failed to parse the region for bucket 'wh'` and every
    protocol test turns red for a reason that has nothing to do with the protocol.

    Stubbed rather than mocked into each test, so a test added tomorrow inherits it, and so this file
    keeps asserting one thing. The contract itself — all three returns, both clauses, and the negative
    control — is driven against REAL datasets in
    `test_the_creation_contract_gates_the_service_path.py`. The same stubbing choice is already made for
    the local path in `test_diagnostics_do_not_leak_and_do_not_guess.py`.
    """
    monkeypatch.setattr(CatalogServiceClient, "_contracted", lambda _self, _ns, _ds, uri: uri)


@respx.mock
def test_the_create_body_carries_ZERO_rows() -> None:
    """ "No byte transits the catalog" must stay true on a dataset's FIRST run too.

    The single-step design — send the data and let the catalog create from it — would have made that
    claim false exactly once per dataset, which is the kind of exception that quietly becomes the
    rule.
    """
    respx.post(f"{BASE}/v1/table/bronze$pages/describe").side_effect = [
        httpx.Response(404),
        httpx.Response(200, json={"location": "s3://b/p.lance", "version": 1}),
    ]
    respx.post(f"{BASE}/v1/namespace/bronze/exists").mock(return_value=httpx.Response(404, json={}))
    respx.post(f"{BASE}/v1/namespace/bronze/create").mock(return_value=httpx.Response(200, json={}))
    create = respx.post(f"{BASE}/v1/table/bronze$pages/create").mock(return_value=httpx.Response(200, json={}))

    _client().ensure("bronze", "pages")

    body = create.calls[0].request.content
    reader = pa.ipc.open_stream(pa.BufferReader(body))
    assert reader.read_all().num_rows == 0
    assert create.calls[0].request.headers["content-type"] == "application/vnd.apache.arrow.stream"


@respx.mock
def test_a_create_RACE_is_not_a_failure() -> None:
    """Two chunks of one run can both find the table absent and both try to create it.

    Treating the loser's 409 as an error would fail a run for succeeding.
    """
    respx.post(f"{BASE}/v1/table/bronze$pages/describe").side_effect = [
        httpx.Response(404),
        httpx.Response(200, json={"location": "s3://b/p.lance", "version": 1}),
    ]
    respx.post(f"{BASE}/v1/namespace/bronze/exists").mock(return_value=httpx.Response(404, json={}))
    respx.post(f"{BASE}/v1/namespace/bronze/create").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{BASE}/v1/table/bronze$pages/create").mock(return_value=httpx.Response(409, json={"detail": "exists"}))

    assert _client().ensure("bronze", "pages") == "s3://b/p.lance"


@respx.mock
def test_commit_sends_fragments_as_DICTS_not_as_the_strings_the_plane_carries() -> None:
    """The one conversion, at the wire where it belongs.

    `CommitFragmentsRequest` declares `list[dict]`; the plane transports `json.dumps(f.to_json())`
    because a fragment has to survive a Dapr activity boundary. Sending the strings through would be
    a 422 at the very end of a run, after every unit had already been fetched.
    """
    route = respx.post(f"{BASE}/management/v1/table/bronze$pages/commit").mock(return_value=httpx.Response(200, json={"version": 2, "row_count": 4}))

    version, rows = _client().commit("bronze", "pages", ['{"id": 0}', '{"id": 1}'], read_version=1, run_id="r1")

    assert (version, rows) == (2, 4)
    sent = json.loads(route.calls[0].request.content)
    assert sent["fragments"] == [{"id": 0}, {"id": 1}]
    assert sent["read_version"] == 1


@respx.mock
def test_a_commit_CONFLICT_is_raised_rather_than_swallowed() -> None:
    """409 means another writer committed against the same read_version.

    The recovery is re-read and re-commit, which the activity's own retry performs. Swallowing it
    into a success would report a version this run did not produce and drop its fragments.
    """
    respx.post(f"{BASE}/management/v1/table/bronze$pages/commit").mock(return_value=httpx.Response(409, json={"detail": "conflict"}))

    with pytest.raises(CatalogError, match="conflict"):
        _client().commit("bronze", "pages", ['{"id": 0}'], read_version=1, run_id="r1")


@respx.mock
def test_an_unreachable_catalog_FAILS_the_run() -> None:
    """No silent fallback to a local write.

    A catalog that is down must stop the run, not quietly produce governed data the catalog has never
    heard of — which is unrecoverable in the sense that matters: nothing downstream will ever be told.
    """
    respx.post(f"{BASE}/v1/table/bronze$pages/describe").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(CatalogError, match="unreachable"):
        _client().ensure("bronze", "pages")


def test_the_chooser_defaults_to_LOCAL_and_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit opt-in, not "use the catalog if a URL happens to resolve".

    Inferring it from reachability means a catalog outage silently downgrades every run to a local
    write — the failure mode this client exists to remove.
    """
    from ingest.catalog import LocalCatalog

    monkeypatch.delenv("RASK_INGEST_USE_CATALOG", raising=False)
    assert isinstance(build_catalog(BRONZE), LocalCatalog)

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "true")
    assert isinstance(build_catalog(BRONZE), CatalogServiceClient)


def test_a_malformed_fragment_does_not_zero_the_whole_count() -> None:
    """One unreadable manifest must cost one fragment's rows, not the run's entire reported progress."""
    from ingest.runtime import _rows_in

    assert _rows_in(['{"physical_rows": 2}', "not json", '{"physical_rows": 2}']) == 4


@respx.mock
def test_an_EXISTING_namespace_is_not_re_created(respx_allows_unused_routes) -> None:
    """The defect this probe exists for, pinned.

    Where namespaces are warehouse-scoped, `POST /v1/namespace/{id}/create` on an already-bound
    namespace is refused by `require_warehouse_scoped` before the catalog reaches its already-exists
    check:

        top-level namespace 'lane' must belong to a warehouse

    So a correctly provisioned tenant made every ingest run fail at the namespace step — measured
    in-cluster after the lane had provisioned project > warehouse > namespace and got 200 for each.
    An existing namespace must be left alone.
    """
    respx.post(f"{BASE}/v1/table/lane$pages/describe").side_effect = [
        httpx.Response(404),
        httpx.Response(200, json={"location": "s3://b/lane/pages.lance", "version": 1}),
    ]
    respx.post(f"{BASE}/v1/namespace/lane/exists").mock(return_value=httpx.Response(200, json={}))
    create = respx.post(f"{BASE}/v1/namespace/lane/create").mock(
        return_value=httpx.Response(400, json={"detail": "top-level namespace 'lane' must belong to a warehouse"})
    )
    respx.post(f"{BASE}/v1/table/lane$pages/create").mock(return_value=httpx.Response(200, json={}))

    _client().ensure("lane", "pages")

    assert not create.called, "an existing namespace was re-created — the warehouse guard refuses that"


# ── the absent-table case: a read door cannot say "absent", so it must not be believed ──────────────


@respx.mock
def test_a_403_on_describe_means_TRY_CREATE_not_give_up() -> None:
    """THE BUG THAT MADE A NEW BRONZE TABLE IMPOSSIBLE.

    `ensure` probed with `describe` and treated ONLY 404 as absent. The catalog answers **403** for a
    table that does not exist — a table with no tuples cannot satisfy `can_get_metadata`, and a read
    door that distinguished ABSENT from HIDDEN would be an existence oracle for table names. So the
    probe raised and `_create_empty` was never reached. Measured against the deployed catalog as
    `service-ingest`, 2026-08-06:

        ABSENT  exists -> 403      ABSENT  describe -> 403
        EXISTS  exists -> 200      EXISTS  describe -> 200 {location...}

    Every ingest run that ever succeeded did so against a table someone had already created. This was
    NOT a UI problem: the service-token path failed identically.
    """
    describe = respx.post(f"{BASE}/v1/table/bind86-bronze$brandnew/describe").mock(
        return_value=httpx.Response(403, json={"title": "PermissionDeniedError", "detail": "can_get_metadata required on table:bind86-bronze$brandnew"})
    )
    respx.post(f"{BASE}/v1/namespace/bind86-bronze/exists").mock(return_value=httpx.Response(200))
    create = respx.post(f"{BASE}/v1/table/bind86-bronze$brandnew/create").mock(
        return_value=httpx.Response(200, json={"location": "s3://bind86-wh/abc_bind86-bronze$brandnew", "version": 1})
    )

    assert _client().ensure("bind86-bronze", "brandnew") == "s3://bind86-wh/abc_bind86-bronze$brandnew"
    assert describe.called, "the probe must still run — an existing table must not be re-created"
    assert create.called, "the 403 was treated as fatal and create was never attempted"


@respx.mock
def test_a_409_then_a_403_is_reported_as_an_AUTHORIZATION_gap_on_an_existing_table() -> None:
    """The one case the fall-through must NOT paper over.

    409 means the table exists; a 403 from the re-describe means this identity genuinely cannot see it.
    That is a real permission gap on an EXISTING table, and reporting it as "created but no location"
    (the old message) sends a reader hunting a catalog bug. The message must name the relation and the
    object, because that is the fix.
    """
    respx.post(f"{BASE}/v1/table/bind86-bronze$hidden/describe").mock(return_value=httpx.Response(403, json={}))
    respx.post(f"{BASE}/v1/namespace/bind86-bronze/exists").mock(return_value=httpx.Response(200))
    respx.post(f"{BASE}/v1/table/bind86-bronze$hidden/create").mock(return_value=httpx.Response(409, json={}))

    with pytest.raises(CatalogError, match="already exists but this identity cannot describe it"):
        _client().ensure("bind86-bronze", "hidden")


@respx.mock
def test_an_EXISTING_visible_table_is_never_re_created(respx_allows_unused_routes) -> None:
    """The fall-through must not turn every run into a create attempt. A describable table short-circuits
    before the namespace probe, which is also what keeps a run cheap on the common path."""
    respx.post(f"{BASE}/v1/table/bind86-bronze$there/add_columns").mock(return_value=httpx.Response(400, json={"detail": "column etag already exists"}))
    respx.post(f"{BASE}/v1/table/bind86-bronze$there/describe").mock(return_value=httpx.Response(200, json={"location": "s3://wh/there", "version": 7}))
    create = respx.post(f"{BASE}/v1/table/bind86-bronze$there/create").mock(return_value=httpx.Response(200, json={}))

    assert _client().ensure("bind86-bronze", "there") == "s3://wh/there"
    assert not create.called, "an existing table was re-created — the create door would 409, but the run should never ask"


@respx.mock
def test_a_NON_authz_error_from_describe_is_still_fatal() -> None:
    """Only 403/404 mean 'no location for you'. A 500 is the catalog being broken, and swallowing it
    into a create attempt would turn an outage into a confusing create failure."""
    respx.post(f"{BASE}/v1/table/bind86-bronze$boom/describe").mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(CatalogError, match="catalog refused describe"):
        _client().ensure("bind86-bronze", "boom")
