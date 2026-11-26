"""The estate table listing walks every warehouse, and one that cannot be read costs only its own tables.

`GET /v1/table` carries no ``{id}`` to route by, so `list_all_tables` walks each warehouse a name may begin with (the
estate's own and every registered one) through that warehouse's own directory catalog, from the warehouse's row down
([[LH-593]]). A namespace dropped between its parent's listing and its own is left out, since it holds no table. Any
other failure, the store not answering or refusing, a warehouse that cannot be reached, proves nothing about the tables
it hides, so the listing is answered 503, the spec's ServiceUnavailable, never a short 200.

DRIVEN THROUGH `GET /v1/table`, over real warehouse records: the property is the merge of the per-warehouse walks,
which a test of one walk alone cannot see.
"""

import logging
from typing import cast

import httpx2
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from lance_namespace import (
    InternalError,
    LanceNamespace,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
    NamespaceNotFoundError,
)

from catalog.api.authz.context import AuthzContext, get_authz_context
from catalog.api.dependencies import get_resolver, get_storage_options
from catalog.api.v1.endpoints import tables as t_ep
from catalog.core.config import Settings
from lakehouse.problems.ns_errors import install_problem_handlers
from lakehouse.storage.options import StorageOptions
from tests import seeding
from tests.catalog_settings import catalog_settings
from tests.unit.named_resolver import NamedResolver


class _DirLike:
    """One warehouse's directory catalog: ``tables`` and ``children`` keyed by the parent's full name, and ``unreadable``
    a namespace whose ``list_tables`` raises ``fault``."""

    def __init__(self, tables: dict[str, list[str]], children: dict[str, list[str]], *, unreadable: str = "", fault: Exception | None = None) -> None:
        self._tables = tables
        self._children = children
        self._unreadable = unreadable
        self._fault = fault

    def list_namespaces(self, req: ListNamespacesRequest) -> ListNamespacesResponse:
        return ListNamespacesResponse(namespaces=list(self._children.get("$".join(req.id or []), [])))

    def list_tables(self, req: ListTablesRequest) -> ListTablesResponse:
        name = "$".join(req.id or [])
        if name == self._unreadable and self._fault is not None:
            raise self._fault
        return ListTablesResponse(tables=list(self._tables.get(name, [])))


_ESTATE = _DirLike({"lance_catalog$bronze": ["pages"]}, {"lance_catalog": ["bronze"]})
_MEDIA = {"media$bronze": ["chunks", "documents"], "media$silver": ["consensus"]}
_MEDIA_CHILDREN = {"media": ["bronze", "silver"]}
#: The store not answering a listing, in pylance's flattened words (`lakehouse.storage.outage`, measured on pylance 12.0.0).
_NO_ANSWER = OSError("LanceError(IO): Generic S3 error: Error performing GET http://store/media-bkt in 3.0s - HTTP error: error sending request")
#: The store refusing the catalog's credential, in the native backend's words (`lakehouse.storage.outage`, measured on pylance 12.0.0).
_REFUSED = InternalError('IO { source: PermissionDenied { path: "media$silver", source: RetryError(... inner: Status { status: 403, ...')


def _settings(tmp_path: object) -> Settings:
    return catalog_settings(root=f"file://{tmp_path}", registry_root=f"file://{tmp_path}")


def _get(monkeypatch: pytest.MonkeyPatch, tmp_path: object, catalogs: dict[str, _DirLike | Exception]) -> httpx2.Response:
    """`GET /v1/table` over the estate's catalog and a registry holding a record for every other warehouse in ``catalogs``.

    Each warehouse resolves to its own catalog, or raises for one that cannot be reached, through the seam the route
    uses, so the merge under test is the production one.
    """
    settings = _settings(tmp_path)
    for warehouse_id in catalogs:
        if warehouse_id != settings.estate_warehouse:
            record = {"id": warehouse_id, "bucket": f"{warehouse_id}-bkt", "root_uri": f"s3://{warehouse_id}-bkt", "project": "acme", "created_at": "t"}
            seeding.put_warehouse(settings.registry_root, StorageOptions(), record)

    async def _resolve(request: Request, settings: Settings, warehouse_id: str) -> LanceNamespace:
        del request, settings
        target = catalogs[warehouse_id]
        if isinstance(target, Exception):
            raise target
        return cast(LanceNamespace, target)

    monkeypatch.setattr(t_ep, "namespace_for_warehouse", _resolve)
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(t_ep.spec_router)
    application.dependency_overrides[get_authz_context] = lambda: AuthzContext(client=None, settings=settings, token=None)
    application.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    application.dependency_overrides[get_resolver] = lambda: NamedResolver()
    with TestClient(application, raise_server_exceptions=False) as client:
        return client.get("/v1/table")


def _list(monkeypatch: pytest.MonkeyPatch, tmp_path: object, catalogs: dict[str, _DirLike | Exception]) -> list[str]:
    answer = _get(monkeypatch, tmp_path, catalogs)
    assert answer.status_code == 200, answer.text
    return answer.json()["tables"]


def test_a_namespace_dropped_mid_walk_is_left_out_of_the_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    """A namespace its parent listed and that is gone by its own listing holds no table, so the rest is answered."""
    dropped = NamespaceNotFoundError("Namespace not found: media$silver")
    got = _list(monkeypatch, tmp_path, {"lance_catalog": _ESTATE, "media": _DirLike(_MEDIA, _MEDIA_CHILDREN, unreadable="media$silver", fault=dropped)})

    assert got == ["lance_catalog$bronze$pages", "media$bronze$chunks", "media$bronze$documents"]


@pytest.mark.parametrize(
    "media",
    [
        pytest.param(_DirLike(_MEDIA, _MEDIA_CHILDREN, unreadable="media$silver", fault=_NO_ANSWER), id="a-namespace"),
        pytest.param(_NO_ANSWER, id="a-warehouse"),
        pytest.param(_DirLike(_MEDIA, _MEDIA_CHILDREN, unreadable="media$silver", fault=_REFUSED), id="a-namespace-the-store-refuses"),
        pytest.param(RuntimeError("warehouse deactivated"), id="a-warehouse-that-cannot-be-reached"),
    ],
)
def test_a_walk_that_fails_is_answered_unavailable_not_a_short_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: object, media: _DirLike | Exception) -> None:
    """A failure proves nothing about the tables it hides, so the listing is the spec's ServiceUnavailable."""
    answer = _get(monkeypatch, tmp_path, {"lance_catalog": _ESTATE, "media": media})

    assert (answer.status_code, answer.json().get("code")) == (503, 17), answer.text
