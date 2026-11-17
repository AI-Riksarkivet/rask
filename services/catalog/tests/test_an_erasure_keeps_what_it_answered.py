"""What an erasure answers stays true beside another caller's ref and through a verification cut short (ADR 0233).

Each test drives one candidate finding of the erasure model (`specs/catalog/Erasure.tla`) through the erasure doors, the
tag door and the release's tick on one app, against real pylance on ``tmp_path``: a step the model's trace ends at
fails once (the read that names a dropped tag's ref, the release's bound), or another caller acts in the grace period,
and the claim is judged by what the doors then answer. A verification that dies at that step leaves the same state as
one whose step fails.

Every test is marked xfail, strictly: each reproduces a defect of the catalog, confirmed 2026-11-17, whose fix is the
owner's to schedule. The day a fix lands the test passes and the strict marker turns the run red, so the marker goes
with the fix. ``raises=AssertionError`` keeps a scenario that no longer sets up (``pytest.fail``) a failure of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, LanceNamespace
from tables_by_name import open_by_name

from catalog.api import erasure_release as release_tick
from catalog.api.dependencies import get_clock, get_control_emitter, get_namespace, get_storage_options
from catalog.api.security import authenticate
from catalog.api.ticks import Tick
from catalog.api.v1.endpoints import erasure as door
from catalog.api.v1.endpoints import tags
from catalog.core.config import Settings, get_settings
from catalog.core.identifiers import with_minted_uuid
from catalog.services import dataplane, erasure_release
from catalog.services.dataplane import create_table, read_arrow_body
from lakehouse.base_registry import BaseRegistry
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from lakehouse.ns_errors import install_problem_handlers
from lakehouse.storage_options import StorageOptions


_PREDICATE = "pii = 'alice'"
_TABLE_ID = ["db", "subjects"]
_TABLE = "db$subjects"


def _confirmed(finding: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{finding}: ADR 0233, confirmed 2026-11-17; the fix is the owner's to schedule")


def _rows(*names: str) -> pa.Table:
    return pa.table({"pii": pa.array(list(names))})


def _created(root: Path) -> LanceNamespace:
    """``subjects`` at v1, alice and bob, through the catalog's create path, its claim on the control root beside ``root``."""
    ns = lance_namespace.connect("dir", {"root": str(root)})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, _rows("alice", "bob").schema) as writer:
        writer.write_table(_rows("alice", "bob"))
    ns.create_namespace(CreateNamespaceRequest(id=_TABLE_ID[:-1], properties=with_minted_uuid(None)))
    body = read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=1 << 20)
    registry = BaseRegistry(control_root=str(root.parent / "control"), storage_options={})
    create_table(ns, StorageOptions(), _TABLE_ID, body, root=str(root), mode="create", registry=registry, holder=_TABLE)
    return ns


def _tagged_over_a_plain_delete(root: Path) -> LanceNamespace:
    """A writer's plain delete hides alice at v2, which a run's tag ``trained`` names; v3 adds carol."""
    ns = _created(root)
    open_by_name(ns, StorageOptions(), _TABLE_ID).delete(_PREDICATE)
    open_by_name(ns, StorageOptions(), _TABLE_ID).tags.create("trained", 2)
    open_by_name(ns, StorageOptions(), _TABLE_ID).insert(_rows("carol"))
    return ns


class _Recorder:
    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)

    def actions(self, action: str) -> list[CatalogControlEvent]:
        return [event for event in self.events if event.action == action]


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class _Catalog:
    """The erasure and tag doors on one app, and the release's tick over it."""

    def __init__(self, client: TestClient, release: Callable[[], release_tick.ErasureReleaseTick]) -> None:
        self.client, self.release = client, release

    def record(self) -> str:
        response = self.client.post(f"/management/v1/table/{_TABLE}/erasure", json={"predicate": _PREDICATE, "retain_days": 0})
        assert response.status_code == 202, response.text
        return str(response.json()["obligation_id"])

    def verify(self, obligation_id: str) -> Any:  # noqa: ANN401 — the client's response
        return self.client.post(f"/management/v1/table/{_TABLE}/erasure/{obligation_id}/verify", json={"predicate": _PREDICATE})


@contextmanager
def _catalog(ns: LanceNamespace, tmp_path: Path, *, emitter: _Recorder, clock: _Clock) -> Generator[_Catalog]:
    settings = Settings(
        s3_access_key_id="k",
        s3_secret_access_key="s",
        LANCE_CONTROL_ROOT=str(tmp_path / "control"),
        LANCE_ERASURE_RELEASE_GRACE_DAYS=7,
    )
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.include_router(tags.router)

    async def _owners(_obj: str) -> list[str]:
        return ["user:dex~olivia"]

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: ns
    application.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    application.dependency_overrides[authenticate] = lambda: PersonPrincipal(
        principal=Principal(idp_id="dex", subject="alice@corp"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)
    )
    application.dependency_overrides[get_control_emitter] = lambda: emitter
    application.dependency_overrides[door.get_table_owners] = lambda: _owners
    application.dependency_overrides[get_clock] = lambda: clock
    with TestClient(application, raise_server_exceptions=False) as client:

        def release() -> release_tick.ErasureReleaseTick:
            assert client.portal is not None, "the release ticks inside the app's lifespan"
            return client.portal.call(release_tick.tick, Tick(application, settings, clock=clock, control=cast(Any, emitter)))

        yield _Catalog(client, release)


def _fails_once(monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> None:
    """Make ``owner.name`` raise once, as a store that does not answer that one read."""
    real, pending = getattr(owner, name), [True]

    def _flaky(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the wrapped function's own types
        if pending:
            pending.clear()
            raise OSError("the store did not answer")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, _flaky)


@_confirmed("ErasureCopyTag")
def test_a_released_erasure_whose_release_ran_leaves_the_subject_on_no_ref(tmp_path: Path, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    # The release deletes the refs the verification named; a tag another caller puts on the residual version in the
    # grace period is not among them (erasure_release._plan reads again only the subtree of the branches it names).
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    with _catalog(ns, tmp_path, emitter=emitter, clock=clock) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        settled = catalog.verify(obligation).json()
        if (settled["obligation"]["state"], [pin["ref"] for pin in settled["report"]["pinned_by"]]) != ("released", ["tag:trained"]):
            pytest.fail(f"the erasure did not settle on its pin: {settled}")
        copied = catalog.client.post(f"/v1/table/{_TABLE}/tags/create", json={"tag": "copy", "version": 2})
        assert copied.status_code == 200, copied.text
        clock.now = datetime.fromisoformat(settled["report"]["release"]["due_at"]) + timedelta(seconds=1)
        catalog.release()
        again = catalog.verify(catalog.record()).json()["report"]

    assert (again["complete"], again["residual_versions"]) == (True, []), (
        f"the first obligation released, its release ran, and {again['pinned_by']} keeps {again['residual_versions']}"
    )


@_confirmed("ErasureTagEventLost")
def test_every_tag_an_erasure_drops_is_announced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    # The verification drops `pinned`, whose version answers the subject, and the door fails reading the ref it pinned for
    # the announcement: the next verification finds the tag gone, so nothing drops or announces it again.
    ns = _created(tmp_path / "data")
    open_by_name(ns, StorageOptions(), _TABLE_ID).tags.create("pinned", 1)
    emitter, clock = _Recorder(), _Clock()
    with _catalog(ns, tmp_path, emitter=emitter, clock=clock) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        _fails_once(monkeypatch, dataplane, "tag_event_ref")
        if (cut := catalog.verify(obligation)).status_code == 200:
            pytest.fail(f"the verification did not fail at the tag's announcement: {cut.text}")
        if "pinned" in open_by_name(ns, StorageOptions(), _TABLE_ID).tags.list():
            pytest.fail("the verification did not drop the tag before it failed")
        finished = catalog.verify(obligation)

    assert [event.extra["tag"] for event in emitter.actions("table_tag_deleted")] == ["pinned"], (
        f"the obligation is {finished.json()['obligation']['state']} and the tag it dropped was never announced"
    )


@_confirmed("ErasureNoticeLost")
def test_everyone_a_release_costs_is_told_before_it_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    # _schedule_release writes the release's record, then fails before its notices; the next verification meets the record,
    # takes it as one whose people were told, and releases the obligation with nobody told.
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    with _catalog(ns, tmp_path, emitter=emitter, clock=clock) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        _fails_once(monkeypatch, erasure_release, "completion_bound")
        if (cut := catalog.verify(obligation)).status_code == 200:
            pytest.fail(f"the verification did not fail between the release's record and its notices: {cut.text}")
        finished = catalog.verify(obligation).json()

    told = sorted(event.extra["subject"] for event in emitter.actions("erasure_release_scheduled"))
    assert told == ["user:dex~alice%40corp", "user:dex~olivia"], f"the obligation is {finished['obligation']['state']} and the release told {told}"
