"""A fenced door asks its caller's authority before it writes anything, and again on the incarnation its fence read
(ADR 0241 §8 B2, §9 P4; `GrantsIntruderFenced`, `GrantsNoRecheck`).

A door writing on or naming a project or warehouse asks its gate with no write, opens a fence on the object (a ticket of
its own, then the incarnation standing), and asks the gate again: the first answer was given on whatever incarnation
stood when it was asked, and only the second one was given while the fence the Write commits under stood. Each test
holds a door between its first ask and its fence while the object is deleted and another tenant makes one again under
its id, then releases it: refused, and nothing it would have written stands. Driven on the deployed shape: the real app
with authorization on (a loopback Dex, a real OpenFGA store of the test's own) over an S3 store.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from catalog.api import fga_deps
from lakehouse.governed import fga


IDP, ROOT = "dex", "estate:rask"


def _admins(estate: Any, *people: str) -> list[dict[str, str]]:  # noqa: ANN401 — the conftest's Estate
    for person in people:
        estate.store.write((fga.user_object(f"{IDP}~{person}"), "admin", ROOT))
    return [estate.bearer(person) for person in people]


class _HeldFence:
    """Hold the first fence a door opens on ``obj``, after its first ask and before its ticket, until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, obj: str) -> None:
        self.arrived, self.release = threading.Event(), threading.Event()
        real = fga_deps._opened

        @asynccontextmanager
        async def _opened(client: Any, settings: Any, opened: str, token: Any) -> AsyncGenerator[fga_deps.DoorFence]:  # noqa: ANN401 — _opened's own arguments
            if opened == obj and not self.arrived.is_set():
                self.arrived.set()
                assert await asyncio.to_thread(self.release.wait, 30), "the held door was never released"
            async with real(client, settings, opened, token) as door:
                yield door

        monkeypatch.setattr(fga_deps, "_opened", _opened)


def _run(estate: Any, method: str, path: str, headers: dict[str, str], **kwargs: Any) -> tuple[threading.Thread, list[int]]:  # noqa: ANN401
    answered: list[int] = []
    door = threading.Thread(target=lambda: answered.append(estate.client.request(method, path, headers=headers, **kwargs).status_code))
    door.start()
    return door, answered


def test_a_namespace_decided_on_a_warehouse_deleted_and_made_again_before_its_fence_opened_is_made_nowhere(
    estate: Any,  # noqa: ANN401 — the conftest's Estate
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """alice's create of ``wh-a$sales`` passes the router's gate on her warehouse and is held before its fence opens;
    ``wh-a`` is deleted with its bucket and erin makes ``wh-a`` again in her project on that bucket. Released, the fence
    reads erin's incarnation, and the second ask, on it, refuses alice: no namespace is made in erin's warehouse and alice
    owns nothing there (``fga_deps.parent_fence``). Without the second ask the create answers 200 and alice's namespace
    stands in erin's warehouse, seeded under the fence of erin's incarnation."""
    alice, erin = _admins(estate, "alice", "erin")
    for project, by in (("acme", alice), ("beta", erin)):
        assert estate.client.post("/management/v1/projects", json={"id": project}, headers=by).status_code == 200
    made = estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme", "bucket": "wh-a-bucket"}, headers=alice)
    assert made.status_code == 200, made.text
    held = _HeldFence(monkeypatch, "warehouse:wh-a")
    door, answered = _run(estate, "POST", "/v1/namespace/wh-a$sales/create", alice, json={})
    assert held.arrived.wait(30), "alice's namespace create never reached its fence"
    steps = [
        estate.client.delete("/management/v1/warehouses/wh-a", params={"purge_bucket": "true"}, headers=alice),
        estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "beta", "bucket": "wh-a-bucket"}, headers=erin),
    ]
    held.release.set()
    door.join(30)

    assert [step.status_code for step in steps] == [200, 200], [step.text for step in steps]
    listed = estate.client.get("/v1/namespace/wh-a/list", headers=erin)
    edges = [t for t in estate.store.tuples() if "warehouse:wh-a" in (t[0], t[2]) and t[1] in ("parent", "child")]
    owned = [t for t in estate.store.tuples() if t[0] == fga.user_object(f"{IDP}~alice") and t[1] == "owner" and t[2].startswith("namespace:")]
    assert (answered, listed.json().get("namespaces"), edges, owned) == ([403], [], [], []), (answered, listed.text)


def test_a_grant_decided_on_a_project_deleted_and_made_again_before_its_fence_opened_lands_nowhere(
    estate: Any,  # noqa: ANN401 — the conftest's Estate
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """alice's grant of bob's membership passes the members door's first ask and is held before its fence opens; acme is
    deleted and erin makes acme again. Released, the fence reads erin's incarnation, and the second ask, on it, refuses
    alice: bob is no member of erin's acme (``fga_deps.door_fence``)."""
    alice, erin = _admins(estate, "alice", "erin")
    assert estate.client.post("/management/v1/projects", json={"id": "acme"}, headers=alice).status_code == 200
    held = _HeldFence(monkeypatch, "project:acme")
    door, answered = _run(estate, "PUT", "/management/v1/projects/acme/members", alice, json={"user": f"{IDP}~bob", "relation": "member"})
    assert held.arrived.wait(30), "alice's grant never reached its fence"
    steps = [
        estate.client.delete("/management/v1/projects/acme", headers=alice),
        estate.client.post("/management/v1/projects", json={"id": "acme"}, headers=erin),
    ]
    held.release.set()
    door.join(30)

    assert [step.status_code for step in steps] == [200, 200], [step.text for step in steps]
    assert (answered, (fga.user_object(f"{IDP}~bob"), "member", "project:acme") in estate.store.tuples()) == ([403], False), answered


def _changes(estate: Any, *kinds: str) -> int:  # noqa: ANN401 — the conftest's Estate
    """How many tuple changes the store's changelog holds on objects of ``kinds``."""
    total = 0
    for kind in kinds:
        token = ""
        while True:
            page = httpx.get(f"{estate.store.url}/stores/{estate.store.store_id}/changes", params={"type": kind, "continuation_token": token}).json()
            if not page.get("changes"):
                break
            total += len(page["changes"])
            token = page["continuation_token"]
    return total


def test_a_caller_the_warehouse_authorizes_for_nothing_creates_no_namespace_in_it_having_written_nothing(estate: Any) -> None:  # noqa: ANN401
    """mallory, who holds nothing on alice's warehouse, asks to create a namespace directly in it: the router's gate
    refuses her before ``parent_fence`` opens anything, so the store's changelog holds no ticket and no tuple of hers on
    the warehouse or a fence (`GrantsIntruderFenced`, `OnlyAuthorizedWrites`)."""
    (alice,) = _admins(estate, "alice")
    assert estate.client.post("/management/v1/projects", json={"id": "acme"}, headers=alice).status_code == 200
    made = estate.client.post("/management/v1/warehouses", json={"id": "wh-a", "project": "acme", "bucket": "wh-a-bucket"}, headers=alice)
    assert made.status_code == 200, made.text
    before = _changes(estate, "warehouse", fga.FENCE_TYPE, "namespace")

    refused = estate.client.post("/v1/namespace/wh-a$intrusion/create", json={}, headers=estate.bearer("mallory"))

    assert (refused.status_code, _changes(estate, "warehouse", fga.FENCE_TYPE, "namespace") - before) == (403, 0), refused.text
