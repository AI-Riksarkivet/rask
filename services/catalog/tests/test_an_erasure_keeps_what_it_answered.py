"""What an erasure answers stays true beside another caller's ref and through a verification cut short (ADR 0233).

Each test drives one finding of the erasure model (`specs/catalog/Erasure.tla`) through the erasure doors, the tag and
branch doors, the finish door and the release's tick on one app, against real pylance on ``tmp_path``: a step the
model's trace ends at fails once (the sending of a dropped tag's announcement, the release's bound, the removal of a
filing), or another caller acts in the grace period or while the release runs, and the claim is judged by what the doors
and the table then answer. A verification that dies at that step leaves the same state as one whose step fails. Each
finding was confirmed against the catalog on 2026-11-17 and is fixed (ADR 0233).
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
from catalog.api.announcing import Announcer
from catalog.api.dependencies import get_clock, get_control_emitter, get_namespace, get_storage_options
from catalog.api.security import authenticate
from catalog.api.ticks import Tick
from catalog.api.v1.endpoints import announcement_intents as finish_door
from catalog.api.v1.endpoints import branches, tags
from catalog.api.v1.endpoints import erasure as door
from catalog.api.v1.endpoints.trash_reclaim import require_can_reclaim_trash
from catalog.core.config import Settings, get_settings
from catalog.core.identifiers import with_minted_uuid
from catalog.services import announcement_intents, erasure_release
from catalog.services.dataplane import create_table, read_arrow_body
from lakehouse import clone_sources
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
_REQUESTER, _OWNER = "user:dex~alice%40corp", "user:dex~olivia"
#: Another caller, who puts a ref on the table in the grace period.
_CREATOR_LOGIN, _CREATOR = "bob@corp", "user:dex~bob%40corp"


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
    """The erasure, tag, branch and finish doors on one app, and the release's tick over it."""

    def __init__(self, client: TestClient, release: Callable[[], release_tick.ErasureReleaseTick], control_root: str, signed_in: list[str]) -> None:
        self.client, self.release, self.control_root, self._signed_in = client, release, control_root, signed_in

    @contextmanager
    def signed_in_as(self, login: str) -> Generator[None]:
        """Each request in the block authenticated as ``login`` at the estate's Dex."""
        self._signed_in.append(login)
        try:
            yield
        finally:
            self._signed_in.pop()

    def record(self) -> str:
        response = self.client.post(f"/management/v1/table/{_TABLE}/erasure", json={"predicate": _PREDICATE, "retain_days": 0})
        assert response.status_code == 202, response.text
        return str(response.json()["obligation_id"])

    def verify(self, obligation_id: str) -> Any:  # noqa: ANN401 — the client's response
        return self.client.post(f"/management/v1/table/{_TABLE}/erasure/{obligation_id}/verify", json={"predicate": _PREDICATE})

    def settled(self, obligation_id: str) -> datetime:
        """Verify ``obligation_id`` into a release of ``tag:trained``; its due time."""
        verified = self.verify(obligation_id).json()
        if (verified["obligation"]["state"], [pin["ref"] for pin in verified["report"]["pinned_by"]]) != ("released", ["tag:trained"]):
            pytest.fail(f"the erasure did not settle on its pin: {verified}")
        return datetime.fromisoformat(verified["report"]["release"]["due_at"])

    def finish(self) -> None:
        """What maintenance's hourly audit asks of the catalog: every filing a door left, sent where its change landed."""
        response = self.client.post("/management/v1/announcement-intents/finish")
        assert response.status_code == 200, response.text


@contextmanager
def _catalog(ns: LanceNamespace, tmp_path: Path, *, emitter: _Recorder, clock: _Clock, owners: list[str] | None = None) -> Generator[_Catalog]:
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
    application.include_router(branches.router)
    application.include_router(finish_door.management_router)
    table_owners = owners if owners is not None else [_OWNER]

    async def _owners(_obj: str) -> list[str]:
        return list(table_owners)

    async def _reclaimer() -> None:
        return None

    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: ns
    application.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    signed_in = ["alice@corp"]
    application.dependency_overrides[authenticate] = lambda: PersonPrincipal(
        principal=Principal(idp_id="dex", subject=signed_in[-1]), claims=IDToken(iss="i", sub=signed_in[-1], aud="lance", exp=1, iat=1)
    )
    application.dependency_overrides[get_control_emitter] = lambda: emitter
    application.dependency_overrides[door.get_table_owners] = lambda: _owners
    application.dependency_overrides[get_clock] = lambda: clock
    application.dependency_overrides[require_can_reclaim_trash] = _reclaimer
    with TestClient(application, raise_server_exceptions=False) as client:

        def release() -> release_tick.ErasureReleaseTick:
            assert client.portal is not None, "the release ticks inside the app's lifespan"
            return client.portal.call(release_tick.tick, Tick(application, settings, clock=clock, control=cast(Any, emitter)))

        yield _Catalog(client, release, settings.registry_root, signed_in)


def _fails_once(monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> None:
    """Make ``owner.name`` raise once, as a store that does not answer that one read."""
    real, pending = getattr(owner, name), [True]

    def _flaky(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the wrapped function's own types
        if pending:
            pending.clear()
            raise OSError("the store did not answer")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, _flaky)


#: Another caller's ref on version 2, the residual the verification left to the release, through the doors that make it.
_REF_WRITES = {
    "tag": ("tags/create", {"tag": "copy", "version": 2}),
    "branch": ("branches/create", {"name": "copy", "from_version": 2}),
}


@pytest.mark.parametrize(
    ("ref", "when"),
    [
        pytest.param("tag", "in-the-grace", id="a-tag-made-in-the-grace"),
        pytest.param("branch", "in-the-grace", id="a-branch-cut-in-the-grace"),
        pytest.param("tag", "while-the-release-runs", id="a-tag-written-while-the-release-runs"),
    ],
)
def test_a_released_erasure_whose_release_ran_leaves_the_subject_on_no_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    erasure_job: Any,  # noqa: ANN401 — conftest.ErasureJob
    ref: str,
    when: str,
) -> None:
    # The release deletes the refs the verification named and every ref standing on a residual version, read again when
    # it runs, and reclaims the residual itself before it ends, all under a hold of the table's history no ref write
    # shares (G1): a ref made in the grace goes with the release, a ref write landing while it runs is refused, and the
    # version the subject was in is gone from storage with no later verification.
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    path, body = _REF_WRITES[ref]
    answered: list[int] = []
    with _catalog(ns, tmp_path, emitter=emitter, clock=clock) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        due = catalog.settled(obligation)

        def _write() -> None:
            answered.append(catalog.client.post(f"/v1/table/{_TABLE}/{path}", json=body).status_code)

        if when == "in-the-grace":
            _write()
        else:
            real_delete = erasure_release._delete

            def _delete_beside_a_ref_write(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the wrapped function's own types
                _write()
                return real_delete(*args, **kwargs)

            monkeypatch.setattr(erasure_release, "_delete", _delete_beside_a_ref_write)
        clock.now = due + timedelta(seconds=1)
        catalog.release()

    dataset = open_by_name(ns, StorageOptions(), _TABLE_ID)
    versions = [entry["version"] for entry in dataset.versions()]
    assert 2 not in versions, f"the release ran and version 2 stands ({versions}); the {ref} write answered {answered}, tags {dataset.tags.list()}"


@pytest.mark.parametrize(
    ("ref", "when"),
    [
        pytest.param("tag", "in-the-grace", id="a-tag-made-in-the-grace"),
        pytest.param("branch", "in-the-grace", id="a-branch-cut-in-the-grace"),
        pytest.param("tag", "its-door-fails-after-the-ref", id="its-door-fails-after-the-tag"),
        pytest.param("tag", "while-the-release-is-recorded", id="a-tag-written-while-the-release-is-recorded"),
    ],
)
def test_whoever_puts_a_ref_the_release_takes_is_told_before_it_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    erasure_job: Any,  # noqa: ANN401 — conftest.ErasureJob
    ref: str,
    when: str,
) -> None:
    # The owner's ruling (ADR 0233 §9): another caller puts a tag on version 2, or cuts a branch from it, and the release
    # will take it. The ref door reads the release its hold's take found marked, files the caller's notice before the ref
    # and sends it after (G6): a door that fails after its ref leaves the notice to the finish, and the release waits for
    # it. A ref written while the verification records the release meets its hold (G7): it is refused, or told.
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    path, body = _REF_WRITES[ref]
    answered: list[int] = []

    def _write() -> None:
        with catalog.signed_in_as(_CREATOR_LOGIN):
            answered.append(catalog.client.post(f"/v1/table/{_TABLE}/{path}", json=body).status_code)

    with _catalog(ns, tmp_path, emitter=emitter, clock=clock) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        if when == "while-the-release-is-recorded":
            real_create, pending = erasure_release.create_json, [True]

            def _create_beside_a_ref_write(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the wrapped function's own types
                if pending:
                    pending.clear()
                    _write()
                return real_create(*args, **kwargs)

            monkeypatch.setattr(erasure_release, "create_json", _create_beside_a_ref_write)
        catalog.settled(obligation)
        recorded = erasure_release.scheduled(catalog.control_root, StorageOptions(), obligation)
        assert recorded is not None, "precondition: the release is scheduled"
        if when == "its-door-fails-after-the-ref":
            _fails_once(monkeypatch, Announcer, "send_filed")
        if when != "while-the-release-is-recorded":
            _write()
        clock.now = recorded.due_at + timedelta(seconds=1)
        if when == "its-door-fails-after-the-ref":
            catalog.release()
            monkeypatch.setattr(announcement_intents, "LEASE", timedelta(0))
            catalog.finish()
        told = [(event.extra["subject"], event.extra["due_at"]) for event in emitter.actions("erasure_release_scheduled") if event.extra["subject"] == _CREATOR]
        catalog.release()

    expected = [] if answered == [409] else [(_CREATOR, recorded.due_at.isoformat())]
    assert told == expected, f"the {ref} write answered {answered}, and before the release ran its caller was told {told}"


def test_a_release_never_takes_a_registered_clones_tag(tmp_path: Path, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    # A clone registered in the grace reads version 2 through this table's files, held by the catalog's own tag (ADR 0198
    # §1). The release deletes no clone's tag (G4): it changes nothing while the clone stands, names the clone on the
    # obligation as a verification names one, and tells the release's people again.
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    clone = clone_sources.tag_of(str(tmp_path / "clone"))
    with _catalog(ns, tmp_path, emitter=emitter, clock=clock) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        due = catalog.settled(obligation)
        open_by_name(ns, StorageOptions(), _TABLE_ID).tags.create(clone, 2)
        clock.now = due + timedelta(seconds=1)
        catalog.release()
        listed = catalog.client.get("/management/v1/erasures").json()["obligations"]

    dataset = open_by_name(ns, StorageOptions(), _TABLE_ID)
    reads = dataset.checkout_version(2).count_rows() if clone in dataset.tags.list() else None
    held = [surface["surface"] for obligation in listed for surface in obligation["open_surfaces"] if surface["outcome"] == "held"]
    told = sorted(
        (event.extra["subject"], event.extra["held_by_clones"]) for event in emitter.actions("erasure_release_scheduled") if "held_by_clones" in event.extra
    )
    # Version 2 holds bob, and alice behind the plain delete's deletion vector.
    assert (reads, held, told) == (1, [f"tag:{clone}"], [(_REQUESTER, [clone]), (_OWNER, [clone])]), (
        f"the clone's version reads {reads} rows, the obligation names {held} held, and the release told {told}"
    )


def test_every_tag_an_erasure_drops_is_announced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    # The verification drops `pinned`, whose version answers the subject, and the door fails sending its announcement:
    # the verification filed it before the drop (G3), so the next verification of the obligation sends it.
    ns = _created(tmp_path / "data")
    open_by_name(ns, StorageOptions(), _TABLE_ID).tags.create("pinned", 1)
    emitter, clock = _Recorder(), _Clock()
    with _catalog(ns, tmp_path, emitter=emitter, clock=clock) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        _fails_once(monkeypatch, Announcer, "send_filed")
        if (cut := catalog.verify(obligation)).status_code == 200:
            pytest.fail(f"the verification did not fail at the tag's announcement: {cut.text}")
        if "pinned" in open_by_name(ns, StorageOptions(), _TABLE_ID).tags.list():
            pytest.fail("the verification did not drop the tag before it failed")
        finished = catalog.verify(obligation)

    assert [event.extra["tag"] for event in emitter.actions("table_tag_deleted")] == ["pinned"], (
        f"the obligation is {finished.json()['obligation']['state']} and the tag it dropped was never announced"
    )


def test_everyone_a_release_costs_is_told_before_it_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    # _schedule_release writes the release's record, then fails before its notices, and no verification comes again. It
    # filed the notices before the record (G2): the release tick leaves the release alone while the filing stands, the
    # catalog's finish sends it once its door is past acting, and only then does the release run.
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    with _catalog(ns, tmp_path, emitter=emitter, clock=clock) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        _fails_once(monkeypatch, erasure_release, "completion_bound")
        if (cut := catalog.verify(obligation)).status_code == 200:
            pytest.fail(f"the verification did not fail between the release's record and its notices: {cut.text}")
        recorded = erasure_release.scheduled(catalog.control_root, StorageOptions(), obligation)
        if recorded is None:
            pytest.fail("the verification failed before it recorded the release")
        clock.now = recorded.due_at + timedelta(seconds=1)
        catalog.release()
        monkeypatch.setattr(announcement_intents, "LEASE", timedelta(0))
        catalog.finish()
        catalog.release()

    told = [(event.action, event.extra.get("subject", event.extra.get("tag"))) for event in emitter.events]
    expected = [("erasure_release_scheduled", _REQUESTER), ("erasure_release_scheduled", _OWNER), ("table_tag_deleted", "trained")]
    assert told == expected, f"in this order: {told}"


@pytest.mark.parametrize("second", [pytest.param("meets-the-record", id="it-meets-the-record"), pytest.param("loses-the-create", id="it-loses-the-create")])
def test_every_notice_names_the_deadline_of_the_release_that_stands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    erasure_job: Any,  # noqa: ANN401 — conftest.ErasureJob
    second: str,
) -> None:
    # The first verification records the release and fails before its notices. A table owner is added, and a second
    # verification an hour later meets the record, or reads none and then loses the create to it (the record written
    # between its read and its create), and fails before its own filing is removed; the finish sends whatever both left
    # (G5). Every notice, the new owner's included, names the deadline of the release that stands.
    ns = _tagged_over_a_plain_delete(tmp_path / "data")
    emitter, clock = _Recorder(), _Clock()
    owners = [_OWNER]
    with _catalog(ns, tmp_path, emitter=emitter, clock=clock, owners=owners) as catalog:
        obligation = catalog.record()
        erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
        _fails_once(monkeypatch, erasure_release, "completion_bound")
        if (cut := catalog.verify(obligation)).status_code == 200:
            pytest.fail(f"the first verification did not fail between the release's record and its notices: {cut.text}")
        recorded = erasure_release.scheduled(catalog.control_root, StorageOptions(), obligation)
        if recorded is None:
            pytest.fail("the first verification failed before it recorded the release")
        owners.append("user:dex~nils")
        clock.now += timedelta(hours=1)
        _fails_once(monkeypatch, announcement_intents, "settle")
        if second == "loses-the-create":
            real_read, pending = erasure_release.scheduled, [True]

            def _read_before_the_record(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the wrapped function's own types
                if pending:
                    pending.clear()
                    return None
                return real_read(*args, **kwargs)

            monkeypatch.setattr(erasure_release, "scheduled", _read_before_the_record)
        catalog.verify(obligation)
        monkeypatch.setattr(announcement_intents, "LEASE", timedelta(0))
        catalog.finish()

    told = {(event.extra["subject"], event.extra["due_at"]) for event in emitter.actions("erasure_release_scheduled")}
    due = recorded.due_at.isoformat()
    assert told == {(_REQUESTER, due), (_OWNER, due), ("user:dex~nils", due)}, f"the release is due at {due}; told {sorted(told)}"
