"""The catalog SERVICE client — the swap that lets a commit be seen by anything but this process.

`LocalCatalog` records versions in a Python list. In a cluster that means the run lands its data and
nothing downstream learns of it: the event that wakes a mover is the CATALOG's publication of a new
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


def test_the_table_id_joins_project_and_dataset_with_the_catalog_delimiter() -> None:
    """`bronze$pages`, not `bronze/pages`. A wrong separator addresses a DIFFERENT table rather than
    failing, which is the kind of mistake that only shows up as missing data."""
    assert _client().table_id("bronze", "pages") == "bronze$pages"


@respx.mock
def test_ensure_returns_the_location_the_catalog_VENDS() -> None:
    """The location is the catalog's answer, never composed here — I2.

    Composing `{warehouse}/{project}/{dataset}.lance` from env is exactly the hardcoded path I2
    forbids: two callers with different env compose different locations for the same logical table.
    """
    respx.post(f"{BASE}/v1/table/bronze$pages/describe").mock(
        return_value=httpx.Response(200, json={"location": "s3://governed/bronze/pages.lance", "version": 3})
    )

    assert _client().ensure("bronze", "pages") == "s3://governed/bronze/pages.lance"


@respx.mock
def test_an_absent_table_is_CREATED_empty_and_then_described() -> None:
    """D6's creation two-step: create server-side with zero rows, then read back the location."""
    describe = respx.post(f"{BASE}/v1/table/bronze$pages/describe")
    describe.side_effect = [
        httpx.Response(404, json={"detail": "no such table"}),
        httpx.Response(200, json={"location": "s3://governed/bronze/pages.lance", "version": 1}),
    ]
    respx.post(f"{BASE}/v1/namespace/bronze/create").mock(return_value=httpx.Response(200, json={}))
    create = respx.post(f"{BASE}/v1/table/bronze$pages/create").mock(return_value=httpx.Response(200, json={"version": 1}))

    assert _client().ensure("bronze", "pages") == "s3://governed/bronze/pages.lance"
    assert create.called


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
    route = respx.post(f"{BASE}/v1/table/bronze$pages/commit").mock(return_value=httpx.Response(200, json={"version": 2, "row_count": 4}))

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
    respx.post(f"{BASE}/v1/table/bronze$pages/commit").mock(return_value=httpx.Response(409, json={"detail": "conflict"}))

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


def test_both_catalogs_present_the_SAME_seam() -> None:
    """`finalize_run` must not need to know which one it has.

    The local catalog is path-based and the service is id-based; if they did not agree on `ensure`,
    the swap would be a code change at every call site instead of a config change.
    """
    from ingest.catalog import LocalCatalog

    for catalog in (LocalCatalog(BRONZE), CatalogServiceClient(BRONZE, base_url=BASE)):
        assert callable(catalog.ensure)


@respx.mock
def test_the_NAMESPACE_is_created_before_the_table() -> None:
    """Three steps, not two — and the order matters.

    A table lives IN a namespace, and the namespace is itself a catalog object with its own manifest.
    It is not implied by the table id: `demo$pages` names a namespace `demo` that must already exist,
    and against a fresh catalog the table create fails with NamespaceNotFoundError ("Child namespace
    reads require an existing __manifest dataset"). Nothing in the table endpoints says so; only a
    real catalog does, and only the first time.
    """
    respx.post(f"{BASE}/v1/table/demo$pages/describe").side_effect = [
        httpx.Response(404),
        httpx.Response(200, json={"location": "s3://b/demo/pages.lance", "version": 1}),
    ]
    namespace = respx.post(f"{BASE}/v1/namespace/demo/create").mock(return_value=httpx.Response(200, json={}))
    table = respx.post(f"{BASE}/v1/table/demo$pages/create").mock(return_value=httpx.Response(200, json={}))

    _client().ensure("demo", "pages")

    assert namespace.called, "the namespace was never created — the table create would 404"
    assert table.called
