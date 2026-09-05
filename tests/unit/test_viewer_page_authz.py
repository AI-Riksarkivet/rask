"""The page BYTES stop being public (#90).

`GET /api/page` streamed a bronze page image to anyone who could reach the gateway, and `/api/pages`
listed the volume's contents to anyone. Neither declared an auth dependency. Its neighbour
`datasets.py` was already gated, which made this an inconsistency rather than a stance.

For an archive the consequence is not abstract: these routes are the primary read path for scanned
records, and the estate holds sealed material, donor-restricted collections and records naming living
people. Every rail the catalog grew — protection flags, trash/undrop, tiered credential vending —
governs the TABLE while this route handed out the pixels.

Two rungs, not one. `/api/pages` lists metadata and takes `can_get_metadata`; `/api/page` returns
image bytes and takes `can_read_data`. The model already separates them, and for an archive the
difference is the point: knowing that a volume of sealed records exists is a different permission
from reading it.

The third defect fixed here is the bearer. `_resolve` asked the catalog to resolve a table id while
sending NO Authorization header at all — so on an auth-enabled catalog it 401'd, and where it
succeeded it succeeded as nobody in particular. The caller's bearer already arrives at the viewer
(sealed cookie -> zone BFF -> gateway); it was simply dropped on the floor.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers
from viewer.api.security import READ_DATA, READ_METADATA
from viewer.api.v1.endpoints import pages as pg
from viewer.api.v1.endpoints.pages import router
from viewer.core.config import ViewerSettings, get_viewer_settings


TABLE = "bronze$pages"


def _app(
    *,
    allow: Any,
    seen: list[dict[str, Any]] | None = None,
    subject: str = "gina",
    token: str | None = "caller-jwt",
    http: Any | None = None,
) -> FastAPI:
    async def checker(*, user: str, relation: str, obj: str) -> bool:
        if seen is not None:
            seen.append({"user": user, "relation": relation, "obj": obj})
        return allow if isinstance(allow, bool) else obj in allow

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)

    settings = ViewerSettings.model_validate(
        {
            "RASK_FGA_ENABLED": True,
            "RASK_OIDC_ENABLED": True,
            "RASK_OIDC_ISSUER": "https://issuer.test",
            "RASK_OIDC_AUDIENCE": "rask",
        }
    )
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    app.dependency_overrides[pg.CheckerDep.__metadata__[0].dependency] = lambda: checker
    app.dependency_overrides[pg.CurrentSubject.__metadata__[0].dependency] = lambda: subject
    app.dependency_overrides[pg.RawBearerToken.__metadata__[0].dependency] = lambda: token

    # `storage_options` is a METHOD on the real settings (docs/DECISIONS.md "The Python estate audit" E2: it performs a
    # blocking Dapr secret fetch, and a @property disguised that as a free attribute read). The
    # double has to match, or it tests a shape production does not have. `http` carries the pooled
    # catalog stub `_resolve` posts through (VS-12) — the resolve reuses `state.http` rather than
    # constructing a client per call.
    state = type(
        "_State",
        (),
        {
            "settings": type("S", (), {"catalog_uri": "http://catalog", "storage_options": lambda _self: {}})(),
            "http": http if http is not None else _catalog_ok(),
        },
    )()
    app.dependency_overrides[pg.StateDep.__metadata__[0].dependency] = lambda: state
    return app


@pytest.fixture(autouse=True)
def _fake_dataset(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Stub only the LANCE layer, deliberately leaving `_open` and `_resolve` real.

    The first cut stubbed `_open` itself, which made every bearer assertion in this file vacuous:
    `_resolve` never ran, so no header was ever sent and the tests passed against a route that
    forwarded nothing. Stubbing one layer deeper — `lance.dataset` — keeps the catalog round-trip on
    the real code path, which is the thing being asserted.

    A REAL tiny blob-v2 dataset rather than a hand-built double of the read seam: the routes read
    through Lance's own take-path (`id -> _rowid -> take_blobs`), and a double of an internal seam
    dies silently the moment the read path moves — this file has already had all nine of its gate
    tests error against a double of a function the routes no longer called. Real bytes through the
    real read path cannot drift out from under the gate.
    """
    import lance
    import pyarrow as pa
    from lance import blob_array, blob_field

    uri = str(tmp_path_factory.mktemp("authz") / "pages.lance")
    # The governed-tier columns the LISTING projects (_PAGE_COLUMNS) must exist, or the listing
    # route fails on schema rather than deciding authorization — which is what this file is for.
    table = pa.table(
        {
            "id": pa.array([0, 1], pa.int64()),
            "source_uri": pa.array(["iiif://vol/0", "iiif://vol/1"], pa.string()),
            "stage": pa.array(["bronze", "bronze"], pa.string()),
            "payload": blob_array([_JPEG, None]),
        },
        schema=pa.schema([pa.field("id", pa.int64()), pa.field("source_uri", pa.string()), pa.field("stage", pa.string()), blob_field("payload")]),
    )
    lance.write_dataset(table, uri, data_storage_version="2.2", enable_stable_row_ids=True)
    real = lance.dataset(uri)
    monkeypatch.setattr(pg.lance, "dataset", lambda *_a, **_kw: real)


def _catalog_ok(seen_headers: list[dict[str, str]] | None = None) -> Any:
    """A pooled catalog client `_resolve` posts through — answers 200 with a location."""

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {"location": "s3://bkt/tbl.lance"}

    class _Client:
        def post(self, _url: str, **kwargs: Any) -> _Resp:
            if seen_headers is not None:
                seen_headers.append(dict(kwargs.get("headers") or {}))
            return _Resp()

    return _Client()


# --- the gate ---------------------------------------------------------------------------------


#: REAL magic bytes, not the string "JPEGBYTES". The route sniffs the payload now instead of asserting
#: `image/jpeg` over whatever it was handed (open_fastapi-audit), so a fixture that is not actually a
#: JPEG would correctly be served as `application/octet-stream` — and the content-type assertion below
#: would be testing the fixture rather than the route.
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00"


def test_page_BYTES_are_refused_without_a_grant() -> None:
    """The defect, stated directly: this used to return the image to anyone."""
    client = TestClient(_app(allow=False))

    r = client.get("/api/page", params={"table": TABLE, "id": 0})

    assert r.status_code == 403
    assert _JPEG not in r.content


def test_the_page_LISTING_is_refused_without_a_grant() -> None:
    """A volume's contents name records someone may not know exist."""
    client = TestClient(_app(allow=False))

    assert client.get("/api/pages", params={"table": TABLE}).status_code == 403


def test_bytes_need_can_read_data_while_the_listing_needs_only_metadata() -> None:
    """The two rungs are DIFFERENT, and that difference is the archival point.

    A caller granted metadata may see that a volume of sealed records exists and how many pages it
    has, and still be refused the pixels. Collapsing both onto one relation would silently widen
    whichever surface got the weaker one.
    """
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    client.get("/api/pages", params={"table": TABLE})
    client.get("/api/page", params={"table": TABLE, "id": 0})

    assert [s["relation"] for s in seen] == [READ_METADATA, READ_DATA]
    assert {s["obj"] for s in seen} == {f"table:{TABLE}"}


def test_the_check_names_the_CATALOG_table_not_an_invented_object() -> None:
    """`table:<id>` — the same object the catalog authorizes on and the annotator writes through."""
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    client.get("/api/page", params={"table": TABLE, "id": 0})

    assert seen == [{"user": "gina", "relation": READ_DATA, "obj": f"table:{TABLE}"}]


def test_a_granted_caller_still_gets_the_bytes() -> None:
    """The gate must not be a wall. Without this the suite would pass with the route deleted."""
    client = TestClient(_app(allow=True))

    r = client.get("/api/page", params={"table": TABLE, "id": 0})

    assert r.status_code == 200
    assert r.content == _JPEG
    assert r.headers["content-type"] == "image/jpeg"


def test_the_denied_caller_is_refused_BEFORE_the_dataset_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Order matters twice over.

    A denied caller must not cause a read of the bytes they were denied — and, less obviously, must
    not be able to use a catalog resolve's timing or error shape to probe which tables exist.

    The catalog is patched to SUCCEED on purpose. Without it this test passed for the wrong reason:
    `_resolve` failed to reach `http://catalog`, so nothing was opened regardless of check order, and
    an inverted-order mutation did not fail it. With a working catalog the check order is the only
    thing that can keep `opened` empty. (Found by mutation, which is exactly what mutation is for.)
    """
    opened: list[str] = []
    monkeypatch.setattr(pg.lance, "dataset", lambda *a, **_kw: opened.append(str(a)))

    r = TestClient(_app(allow=False)).get("/api/page", params={"table": TABLE, "id": 0})

    assert r.status_code == 403
    assert opened == [], "the dataset was opened for a caller who was refused"


# --- the bearer -------------------------------------------------------------------------------


def test_the_CALLERS_bearer_is_forwarded_to_the_catalog() -> None:
    """Not a service credential, and not nothing.

    `raw_bearer`'s own docstring says why a service token is wrong: the catalog checks one relation
    on one `table:` object and injects no row predicate, so a service identity answers 200 for a
    caller with no grant at all. Before this, the header was absent entirely.
    """
    headers: list[dict[str, str]] = []
    client = TestClient(_app(allow=True, token="caller-jwt", http=_catalog_ok(headers)))

    client.get("/api/page", params={"table": TABLE, "id": 0})

    assert headers == [{"Authorization": "Bearer caller-jwt"}]


def test_an_anonymous_caller_sends_no_authorization_header_at_all() -> None:
    """`Bearer None` would be a forged-looking credential; absence is the honest wire."""
    headers: list[dict[str, str]] = []
    client = TestClient(_app(allow=True, token=None, http=_catalog_ok(headers)))

    client.get("/api/page", params={"table": TABLE, "id": 0})

    assert headers == [{}]


def test_a_catalog_401_says_WHICH_credential_problem_it_was() -> None:
    """ "No credential sent" and "credential refused" are different problems with different fixes.

    The route already refused to launder 401/403 into "unknown table" — the annotator lost real
    debugging time to that. Now that a bearer is actually forwarded, the message distinguishes the
    two cases, because "the caller is anonymous" and "the caller was refused" send a reader in
    opposite directions.
    """

    class _Resp:
        status_code = 401

    class _Client:
        def post(self, _url: str, **_kw: Any) -> _Resp:
            return _Resp()

    anon = TestClient(_app(allow=True, token=None, http=_Client())).get("/api/page", params={"table": TABLE, "id": 0})
    held = TestClient(_app(allow=True, token="caller-jwt", http=_Client())).get("/api/page", params={"table": TABLE, "id": 0})

    assert "no bearer was sent" in anon.text
    assert "bearer was refused" in held.text
