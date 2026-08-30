"""The raw S3 object browser stops being public (#90, second half).

`GET /api/objects` (list), `/api/object` (HEAD) and `/api/object/download` (FULL BYTES) shipped with
no token dependency and no authorization. They validated only the store NAME against the registry —
which answers "is this a bucket we know about" and never "may you read it". Their neighbour
`datasets.py` was already FGA-gated, which made this an inconsistency rather than a stance.

WHY AN ESTATE-TIER RELATION AND NOT A PER-STORE ONE. A `store` FGA type would need a parent tuple
per store to be reachable at all, and the four SHIPPED default stores are never registered through
the API — they come from `DEFAULT_STORES` in code, so nothing would write their tuples. The model
would be correct and the gate would deny every caller including the estate owner. A gate that denies
everyone is an outage, not a gate.

Owner tier is not incidental either: the registry's buckets include the external RAW tier and the
observability bucket, both deliberately outside the medallion (R23), so a per-tenant admin must not
reach them. `POST /v1/stores` is already estate-admin gated; this makes reading a store consistent
with registering one rather than strictly weaker.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.exceptions import register_handlers
from viewer.api.security import BROWSE_STORAGE
from viewer.api.v1.endpoints import objects as ob
from viewer.api.v1.endpoints.objects import router
from viewer.core.config import ViewerSettings, get_viewer_settings


STORE = "lance-catalog"
ROOT = "warehouse:lance_catalog"


class _Body:
    """A botocore StreamingBody's read surface, as the download route consumes it."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_chunks(self, chunk_size: int = 1024) -> Any:
        return iter([self._payload[i : i + chunk_size] for i in range(0, len(self._payload), chunk_size)])

    def close(self) -> None:
        return None


class _S3:
    """The boto surface these three routes touch, and nothing else."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def list_objects_v2(self, **_kw: Any) -> dict[str, Any]:
        # `list_objects_v2`, not `get_paginator`: the route makes ONE bounded call now and hands S3's
        # own continuation token back, instead of draining the paginator to exhaustion.
        self._calls.append("list")
        return {"Contents": [{"Key": "a.tif", "Size": 3, "LastModified": None}]}

    def head_object(self, **_kw: Any) -> dict[str, Any]:
        self._calls.append("head")
        return {"ContentLength": 3, "ContentType": "image/tiff", "LastModified": None, "ETag": '"e"'}

    def get_object(self, **_kw: Any) -> dict[str, Any]:
        self._calls.append("get")
        # `iter_chunks` + `close`, not `read`: the download STREAMS now (VS-15) — the route used to
        # materialise the whole object before sending a byte, which a multi-GB object in a registered
        # store turns into an OOM. The bytes this double serves are unchanged.
        return {"ContentType": "image/tiff", "ContentLength": len(b"TIFFBYTES"), "Body": _Body(b"TIFFBYTES")}


def _app(*, allow: Any, seen: list[dict[str, Any]] | None = None) -> FastAPI:
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
    app.dependency_overrides[ob.CheckerDep.__metadata__[0].dependency] = lambda: checker
    app.dependency_overrides[ob.CurrentSubject.__metadata__[0].dependency] = lambda: "gina"
    app.dependency_overrides[ob.SettingsDep.__metadata__[0].dependency] = lambda: settings
    return app


@pytest.fixture(autouse=True)
def _fake_s3(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub only the S3 CLIENT — the registry lookup and the route body stay real."""
    calls: list[str] = []
    monkeypatch.setattr(ob, "_client_for", lambda _name: _S3(calls))
    return calls


# --- the gate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/objects", {"bucket": STORE}),
        ("/api/object", {"bucket": STORE, "key": "a.tif"}),
        ("/api/object/download", {"bucket": STORE, "key": "a.tif"}),
    ],
    ids=["list", "head", "download"],
)
def test_every_object_route_is_refused_without_the_estate_grant(path: str, params: dict[str, str]) -> None:
    """All THREE, not just the download. A key listing names records someone may not know exist."""
    r = TestClient(_app(allow=False)).get(path, params=params)

    assert r.status_code == 403
    assert b"TIFFBYTES" not in r.content


def test_the_bytes_route_does_not_touch_s3_for_a_denied_caller(_fake_s3: list[str]) -> None:
    """Refused BEFORE the S3 call, so a denial costs no request against the object store — and
    cannot be timed to infer whether a key exists."""
    TestClient(_app(allow=False)).get("/api/object/download", params={"bucket": STORE, "key": "a.tif"})

    assert _fake_s3 == [], "S3 was called for a caller who was refused"


def test_a_granted_caller_still_gets_the_bytes() -> None:
    """The gate must not be a wall. Without this the suite passes with the routes deleted."""
    r = TestClient(_app(allow=True)).get("/api/object/download", params={"bucket": STORE, "key": "a.tif"})

    assert r.status_code == 200
    assert r.content == b"TIFFBYTES"


def test_the_check_is_made_against_the_ESTATE_ROOT_not_the_store() -> None:
    """The app-side half of the scoping, promised in `model.fga.yaml`'s own comment.

    `can_browse_storage` resolves on ANY warehouse its owner owns — the model happily says yes for
    a tenant's own warehouse. What makes this an ESTATE privilege is solely that the route names
    `fga_root_object`. If a later edit passed the store or a tenant warehouse instead, the model
    would not complain and the scope would widen in silence. So it is pinned here.
    """
    seen: list[dict[str, Any]] = []
    client = TestClient(_app(allow=True, seen=seen))

    client.get("/api/objects", params={"bucket": STORE})
    client.get("/api/object", params={"bucket": STORE, "key": "a.tif"})
    client.get("/api/object/download", params={"bucket": STORE, "key": "a.tif"})

    assert [s["obj"] for s in seen] == [ROOT, ROOT, ROOT]
    assert {s["relation"] for s in seen} == {BROWSE_STORAGE}
    assert {s["user"] for s in seen} == {"gina"}


def test_an_unregistered_store_is_still_a_404_not_a_403() -> None:
    """The registry check and the authz check answer different questions, and both still run.

    A 403 here would tell an unauthorized caller nothing, but it would also lose the diagnostic the
    storage browser depends on: it lists stores FROM the registry, so if it asks for one, the
    registry said it existed. Authz first, then existence — the caller is authorized estate-wide
    before either answer is given, so the 404 leaks nothing.
    """
    r = TestClient(_app(allow=True)).get("/api/objects", params={"bucket": "no-such-store"})

    assert r.status_code == 404
