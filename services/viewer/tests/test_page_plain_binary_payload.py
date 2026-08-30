"""A plain ``large_binary`` payload column is served, not 500'd — both routes, one answer.

Adversarial re-audit of VS-05's fix (8834b95a): ``table`` is a free caller-supplied catalog id, so a
``payload`` that is a plain ``large_binary`` column rather than blob-v2 is a reachable shape — and
`_page_rows` says exactly that in its own docstring, branching the LISTING on
``is_blob_field(ds.schema.field("payload"))``. But `_take_page` called
``take_blobs("payload", ids=[rowid])`` unconditionally, which raises on a non-blob column: the same
table answered 200 on ``/api/pages`` and 500 on ``/api/page``. A listing that advertises pages the
byte route then refuses to serve is worse than either route failing alone.

A REAL dataset rather than a mock, for the reason the sibling suites give: the branch condition is a
measured pylance behaviour (a scan strips the Arrow extension marker, so the shape question must be
asked of ``ds.schema``), and a double would restate the assumption instead of testing it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers
from viewer.api.v1.endpoints import pages as pg
from viewer.api.v1.endpoints.pages import router
from viewer.core.config import ViewerSettings, get_viewer_settings


TABLE = "bronze$pages"

#: Real JPEG magic so the sniff is exercised on the fixture — a payload that were not actually a
#: JPEG would honestly be served as `application/octet-stream` and the content-type assertion below
#: would be testing the fixture rather than the route.
_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00" + b"\x2a" * 500

#: The id whose payload is null — a registered row that failed to acquire, sitting in the same
#: dataset as the real one so the absence signal is read from among present rows.
NULL_ROW_ID = 1


@pytest.fixture(scope="module")
def dataset_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A bronze-shaped dataset whose ``payload`` is a PLAIN ``large_binary`` column, not blob-v2."""
    table = pa.table(
        {
            "id": pa.array([0, NULL_ROW_ID], pa.int64()),
            "source_uri": pa.array(["iiif://vol/0", "iiif://vol/1"], pa.string()),
            "stage": pa.array(["bronze", "bronze"], pa.string()),
            "payload": pa.array([_JPEG, None], pa.large_binary()),
        }
    )
    path = tmp_path_factory.mktemp("plainpages") / "pages.lance"
    lance.write_dataset(table, str(path))
    return path


def _catalog_ok() -> Any:
    """A pooled catalog client `_resolve` posts through — answers 200 with a location."""

    class _Resp:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {"location": "s3://bkt/tbl.lance"}

    class _Client:
        def post(self, _url: str, **_kw: Any) -> _Resp:
            return _Resp()

    return _Client()


def _app() -> FastAPI:
    """The pages router with the authorization plane satisfied — this file tests the SHAPE branch.

    The gate itself is pinned by `tests/unit/test_viewer_page_authz.py`; granting here keeps a
    permission failure from masquerading as a shape failure.
    """

    async def checker(*, user: str, relation: str, obj: str) -> bool:
        return True

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)

    settings = ViewerSettings.model_validate(
        {
            "LANCE_FGA_ENABLED": True,
            "LANCE_OIDC_ENABLED": True,
            "LANCE_OIDC_ISSUER": "https://issuer.test",
            "LANCE_OIDC_AUDIENCE": "rask",
        }
    )
    app.dependency_overrides[get_viewer_settings] = lambda: settings
    app.dependency_overrides[pg.CheckerDep.__metadata__[0].dependency] = lambda: checker
    app.dependency_overrides[pg.CurrentSubject.__metadata__[0].dependency] = lambda: "gina"
    app.dependency_overrides[pg.RawBearerToken.__metadata__[0].dependency] = lambda: "caller-jwt"
    # `storage_options` is a METHOD on the real settings (it performs a blocking Dapr secret fetch),
    # so the double has to be one too or it tests a shape production does not have.
    state = type(
        "_State",
        (),
        {"settings": type("S", (), {"catalog_uri": "http://catalog", "storage_options": lambda _self: {}})(), "http": _catalog_ok()},
    )()
    app.dependency_overrides[pg.StateDep.__metadata__[0].dependency] = lambda: state
    return app


@pytest.fixture
def _plain_dataset(dataset_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = lance.dataset(str(dataset_path))
    monkeypatch.setattr(pg.lance, "dataset", lambda *_a, **_kw: real)


@pytest.mark.usefixtures("_plain_dataset")
def test_the_listing_answers_for_a_plain_binary_payload() -> None:
    """The `descriptors=False` branch of the listing, pinned: `size` is `len(bytes)` and the null
    signal is the cell's own validity — no descriptor struct exists to read either from."""
    r = TestClient(_app()).get("/api/pages", params={"table": TABLE})

    assert r.status_code == 200
    pages = {p["id"]: p for p in r.json()["pages"]}
    assert pages[0]["has_payload"] is True
    assert pages[0]["size"] == len(_JPEG)
    assert pages[NULL_ROW_ID]["has_payload"] is False
    assert pages[NULL_ROW_ID]["size"] == 0


@pytest.mark.usefixtures("_plain_dataset")
def test_the_bytes_route_serves_a_plain_binary_payload() -> None:
    """The defect, stated directly: the listing 200'd for this table while `/api/page` 500'd on the
    unconditional `take_blobs` of a column that is not a blob column."""
    r = TestClient(_app()).get("/api/page", params={"table": TABLE, "id": 0})

    assert r.status_code == 200
    assert r.content == _JPEG
    assert r.headers["content-type"] == "image/jpeg", "the sniff must run on the plain shape too"


@pytest.mark.usefixtures("_plain_dataset")
def test_a_null_plain_payload_is_the_same_404_as_a_null_blob() -> None:
    """The absence signal must not depend on the column's physical shape."""
    r = TestClient(_app()).get("/api/page", params={"table": TABLE, "id": NULL_ROW_ID})

    assert r.status_code == 404
    assert "has no payload" in r.text


@pytest.mark.usefixtures("_plain_dataset")
def test_an_absent_id_is_a_404_on_the_plain_shape_too() -> None:
    r = TestClient(_app()).get("/api/page", params={"table": TABLE, "id": 999})

    assert r.status_code == 404
    assert "no page with id 999" in r.text
