"""The catalog's doors as maintenance asks them: the maintain tier, answered over each table's own location, and the trash
doors, answered as the catalog's own act on a trash record ([[XC-084]]).

Maintenance signs every write with its table's vend ([[LH-219]]), and the trash is the catalog's to end record by record.
The answers here are shaped as the catalog's are (`mode`, `credentials.storage_options`, `location`; a release's and an
end's counts), with the moto store's pair as the credential: moto enforces no session policy, so what a vend's policy
narrows is the catalog's own tests' claim, and what a test using these holds is maintenance acting on a vend for exactly
its table or record. The vend names the address a client off the cluster reaches the store at, which maintenance
replaces with its own, as it does the real door's ([[LH-489]]). Each stands in for a door rather than making a claim
about it: a pass that asks nothing leaves its route uncalled, and every other request goes where it was sent.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from urllib.parse import quote, unquote

import httpx
import respx
from fastapi.testclient import TestClient

from lakehouse import trash
from lakehouse.storage_options import StorageOptions
from lakehouse.trash import TrashKind


#: Where maintenance is told the catalog's door is (`MAINTENANCE_CATALOG_URL`).
CATALOG_URL = "http://catalog.test"
#: What the vend carries: the moto store's pair, which it takes for any request, and a store address off the cluster.
VENDED = {"aws_access_key_id": "test", "aws_secret_access_key": "test", "aws_endpoint": "http://store.vended.example"}
#: The maintain-tier door's path, the table's id named: `/management/v1/table/{id}/credentials?tier=maintain`.
MAINTAIN_TIER_DOOR = rf"^{CATALOG_URL}/management/v1/table/(?P<table>[^/]+)/credentials"


def described_by(catalog: httpx.Client) -> Callable[[str], str]:
    """The location the catalog ``catalog`` reaches (a `TestClient` or a client of a live one) describes a table at, by its id."""

    def _location(table: str) -> str:
        described = catalog.post(f"/v1/table/{quote(table, safe='$')}/describe", json={})
        assert described.status_code == 200, described.text
        return str(described.json()["location"])

    return _location


@contextmanager
def maintain_tier_door(location_of: Callable[[str], str]) -> Generator[respx.MockRouter]:
    """Answer each maintain-tier vend maintenance asks `CATALOG_URL` for, over the location ``location_of`` gives the table."""

    def _vend(_request: httpx.Request, table: str) -> httpx.Response:
        location = location_of(unquote(table))
        return httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": VENDED}, "location": location})

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=MAINTAIN_TIER_DOOR, params={"tier": "maintain"}).mock(side_effect=_vend)
        router.route().pass_through()
        yield router


#: A trash door's path, its record's kind and uuid named: `/management/v1/trash/{kind}/{uuid}[/<act>]`.
_TRASH_DOOR = rf"^{CATALOG_URL}/management/v1/trash/(?P<kind>table|namespace)/(?P<uuid>[^/?]+)"
#: The reclaim session's answer: the vend's pair, which deletes the record's bytes on moto.
_RECLAIM_SESSION = {"mode": "direct", "credentials": {"storage_options": VENDED}}


def _kind(value: str) -> TrashKind:
    return "namespace" if value == "namespace" else "table"


def _no_record(kind: TrashKind, uuid: str) -> httpx.Response:
    """The catalog's answer for a uuid no trash record holds."""
    return httpx.Response(404, json={"code": 1 if kind == "namespace" else 4, "detail": f"no trashed {kind} holds the uuid {uuid}"})


def end_door(control_root: str, storage_options: StorageOptions) -> Callable[[httpx.Request, str, str], httpx.Response]:
    """The end door `trash_doors` answers with: it ends a record and what it keeps (`lakehouse.trash.end`), as the
    catalog's `DELETE /management/v1/trash/{kind}/{uuid}` does once it judges the record's place empty."""

    def _end(_request: httpx.Request, kind: str, uuid: str) -> httpx.Response:
        if (record := trash.get(control_root, storage_options, uuid, kind=_kind(kind))) is None:
            return _no_record(_kind(kind), uuid)
        trash.end(control_root, storage_options, record, kind=_kind(kind), uuid=uuid)
        return httpx.Response(200, json={"tuples_revoked": 0})

    return _end


@contextmanager
def trash_doors(control_root: str, storage_options: StorageOptions) -> Generator[respx.MockRouter]:
    """Answer each trash door maintenance asks `CATALOG_URL`, acting on the trash at ``control_root`` as the catalog's
    doors act, with no catalog running: the reclaim session is the vend's pair, a release stamps the record (no grant is
    revoked: these suites run with authorization off), an end ends the record and what it keeps (`lakehouse.trash.end`)
    and a refusal's note counts it (`lakehouse.trash.note_refusal`); a uuid no record holds is answered 404, as the
    catalog answers it.

    Whether the catalog would act (its judgement) is its own tests' claim
    (`tests/integration/test_a_write_vend_reaches_only_what_its_rung_may_change.py`). The routes are named
    (`credentials`, `release`, `end`, `refusals`), so a test can answer one otherwise.
    """

    def _absent(kind: TrashKind, uuid: str) -> httpx.Response | None:
        if trash.get(control_root, storage_options, uuid, kind=kind) is not None:
            return None
        return _no_record(kind, uuid)

    def _release(_request: httpx.Request, kind: str, uuid: str) -> httpx.Response:
        if (absent := _absent(_kind(kind), uuid)) is not None:
            return absent
        stamped = trash.mark_released(control_root, storage_options, uuid, kind=_kind(kind))
        return httpx.Response(200, json={"tuples_revoked": 0, "stamped": stamped})

    def _note(request: httpx.Request, kind: str, uuid: str) -> httpx.Response:
        if (absent := _absent(_kind(kind), uuid)) is not None:
            return absent
        reason = str(json.loads(request.content)["reason"])
        return httpx.Response(200, json={"attempts": trash.note_refusal(control_root, storage_options, uuid, kind=_kind(kind), reason=reason)})

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"{_TRASH_DOOR}/credentials$", name="credentials").mock(return_value=httpx.Response(200, json=_RECLAIM_SESSION))
        router.post(url__regex=rf"{_TRASH_DOOR}/release$", name="release").mock(side_effect=_release)
        router.delete(url__regex=rf"{_TRASH_DOOR}$", name="end").mock(side_effect=end_door(control_root, storage_options))
        router.post(url__regex=rf"{_TRASH_DOOR}/refusals$", name="refusals").mock(side_effect=_note)
        router.route().pass_through()
        yield router


@contextmanager
def catalog_doors(catalog: TestClient) -> Generator[respx.MockRouter]:
    """Answer each trash and grant door maintenance asks `CATALOG_URL` with the real catalog app ``catalog``, so its
    judgement and its acts are the catalog's own, but for the reclaim session's credential, which is the vend's pair (the
    app's own vendor vends nothing direct here: what its policy narrows is
    `tests/integration/test_a_write_vend_reaches_only_what_its_rung_may_change.py`'s claim)."""

    def _forward(request: httpx.Request, **_named: str) -> httpx.Response:
        answered = catalog.request(request.method, request.url.raw_path.decode("ascii"), content=request.content, headers={"Content-Type": "application/json"})
        return httpx.Response(
            answered.status_code, headers={"Content-Type": answered.headers.get("content-type", "application/json")}, content=answered.content
        )

    with respx.mock(assert_all_called=False) as router:
        router.post(url__regex=rf"{_TRASH_DOOR}/credentials$", name="credentials").mock(return_value=httpx.Response(200, json=_RECLAIM_SESSION))
        router.route(url__regex=_TRASH_DOOR, name="trash").mock(side_effect=_forward)
        router.route(url__regex=rf"^{CATALOG_URL}/management/v1/grants/", name="grants").mock(side_effect=_forward)
        router.route().pass_through()
        yield router
