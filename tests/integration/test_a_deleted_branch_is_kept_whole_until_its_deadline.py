"""A deleted branch is kept whole for the grace period, and comes back with every commit made on it ([[LH-429]]).

Lance's own branch delete removes `tree/<branch>/`, and recreating the branch where it was cut restores none of its
commits (measured on pylance 14.0.3). Where a warehouse keeps what is dropped for a while, the catalog hides the branch
instead and holds the version it was cut from with a tag, so the parent's cleanup deletes nothing the branch reads.
Driven through the catalog's own doors on a real `dir` root; the branch's name holds a `/`, which its reference file
spells `%2F`.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from tests.integration.lineage_app import catalog_tick, live_catalog


_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
_TABLE = "lance_catalog$db$t"
_BRANCH = "team/work"
_DELETE = f"/v1/table/{_TABLE}/branches/delete"
_RESTORE = f"/management/v1/table/{_TABLE}/branches/restore"
_ERASURE = f"/management/v1/table/{_TABLE}/erasure"


def _rows(*ids: int) -> bytes:
    table = pa.table({"id": pa.array(ids, pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@contextmanager
def _a_table_with_a_branch_holding_a_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, grace_days: str = "1") -> Generator[httpx.Client]:
    """A catalog keeping drops for ``grace_days``, table ``t`` holding ids 1 and 2, and its branch ``team/work``, cut
    there, holding 10 as well."""
    with live_catalog(tmp_path / "catalog", monkeypatch, LANCE_TRASH_GRACE_DAYS=grace_days) as url, httpx.Client(base_url=url, timeout=60) as catalog:
        assert catalog.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert catalog.post(f"/v1/table/{_TABLE}/create", content=_rows(1, 2), headers=_ARROW).status_code == 200
        assert catalog.post(f"/v1/table/{_TABLE}/branches/create", json={"name": _BRANCH}).status_code == 200
        assert catalog.post(f"/v1/table/{_TABLE}/insert", params={"branch": _BRANCH}, content=_rows(10), headers=_ARROW).status_code == 200
        yield catalog


def _root(catalog: httpx.Client) -> Path:
    return Path(catalog.post(f"/v1/table/{_TABLE}/describe", json={"vend_credentials": False}).json()["location"].removeprefix("file://"))


def _branch_ids(catalog: httpx.Client) -> list[int]:
    return sorted(lance.dataset(str(_root(catalog))).checkout_version((_BRANCH, None)).to_table()["id"].to_pylist())


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(days=2)


def test_a_deleted_branch_is_restored_with_the_commits_made_on_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200

        restored = catalog.post(_RESTORE, json={"name": _BRANCH})

        assert (restored.status_code, restored.json(), _branch_ids(catalog)) == (200, {"restored": [_BRANCH]}, [1, 2, 10])


def _create(name: str) -> Callable[[httpx.Client], httpx.Response]:
    return lambda catalog: catalog.post(f"/v1/table/{_TABLE}/branches/create", json={"name": name})


@pytest.mark.parametrize(
    ("ask", "answer"),
    [
        pytest.param(lambda catalog: catalog.post(f"/v1/table/{_TABLE}/branches/list", json={}), (200, None), id="listed nowhere"),
        pytest.param(
            lambda catalog: catalog.post(f"/v1/table/{_TABLE}/insert", params={"branch": _BRANCH}, content=_rows(11), headers=_ARROW),
            (404, 22),
            id="opened by no write",
        ),
        pytest.param(_create(_BRANCH), (409, 19), id="its name is not created over"),
        pytest.param(_create("team"), (409, 19), id="no branch is created around it"),
        pytest.param(_create(f"{_BRANCH}/x"), (409, 19), id="no branch is created inside it"),
        pytest.param(lambda catalog: catalog.post(_RESTORE, json={"name": "never"}), (404, 22), id="a name never deleted is not restored"),
    ],
)
def test_a_deleted_branch_is_a_missing_one_whose_directory_is_still_held(
    ask: Callable[[httpx.Client], httpx.Response], answer: tuple[int, int | None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200

        answered = ask(catalog)

        assert (answered.status_code, answered.json().get("code"), answered.json().get("branches", {})) == (*answer, {})


def test_a_deleted_branch_outlives_the_cleanup_of_the_parent_it_was_cut_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200
        # The parent stops reading the files the branch was cut on, and its history is reclaimed as maintenance reclaims it.
        assert catalog.post(f"/v1/table/{_TABLE}/insert", params={"mode": "overwrite"}, content=_rows(7), headers=_ARROW).status_code == 200
        lance.dataset(str(_root(catalog))).cleanup_old_versions(older_than=timedelta(0), delete_unverified=True, error_if_tagged_old_versions=False)

        restored = catalog.post(_RESTORE, json={"name": _BRANCH})

        assert (restored.status_code, _branch_ids(catalog)) == (200, [1, 2, 10])


def test_a_branch_a_deleted_one_was_cut_from_is_not_deleted_while_that_one_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(f"/v1/table/{_TABLE}/branches/create", json={"name": "fork", "from_branch": _BRANCH}).status_code == 200
        assert catalog.post(_DELETE, json={"name": "fork"}).status_code == 200

        refused = catalog.post(_DELETE, json={"name": _BRANCH})

        assert (refused.status_code, refused.json()["code"], "the deleted branch 'fork' was cut from it" in refused.json()["detail"]) == (409, 19, True)


@pytest.mark.parametrize(
    ("grace_days", "hiding", "purge"),
    [
        pytest.param("0", "true", None, id="the warehouse keeps nothing"),
        pytest.param("1", "false", None, id="hiding branches is switched off"),
        pytest.param("1", "true", "true", id="the caller asks for it gone now"),
    ],
)
def test_a_branch_delete_that_keeps_nothing_destroys_the_branch_at_once(
    grace_days: str, hiding: str, purge: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANCE_BRANCH_TRASH_ENABLED", hiding)
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch, grace_days=grace_days) as catalog:
        kept = _root(catalog) / "tree" / _BRANCH

        deleted = catalog.post(_DELETE, params={} if purge is None else {"purge": purge}, json={"name": _BRANCH})

        assert (deleted.status_code, kept.exists(), catalog.post(_RESTORE, json={"name": _BRANCH}).status_code) == (200, False, 404)


def test_a_deleted_branch_past_its_deadline_is_deleted_for_good(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from catalog.api import branch_trash
    from catalog.core.config import get_settings

    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200
        root = _root(catalog)
        # A record that does not read as one, of another table, holds up nobody's deadline.
        unreadable = Path(get_settings().registry_root) / "_branch_trash" / ("0" * 32) / "x.json"
        unreadable.parent.mkdir(parents=True)
        unreadable.write_text("{}")

        ticked = catalog_tick(str(catalog.base_url), branch_trash.tick, clock=_later)

        left = ((root / "tree" / _BRANCH).exists(), list(lance.dataset(str(root)).tags.list()), catalog.post(_RESTORE, json={"name": _BRANCH}).status_code)
        assert (ticked.purged, ticked.failed, left) == (1, 0, (False, [], 404))


def test_a_deleted_branch_inside_its_grace_period_is_left_by_the_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from catalog.api import branch_trash

    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200

        ticked = catalog_tick(str(catalog.base_url), branch_trash.tick)

        assert (ticked.purged, catalog.post(_RESTORE, json={"name": _BRANCH}).status_code, _branch_ids(catalog)) == (0, 200, [1, 2, 10])


def test_a_delete_that_died_before_the_reference_went_leaves_a_branch_that_stands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from catalog.api import branch_trash as trash_tick
    from catalog.core.config import get_settings
    from lakehouse.records import branch_trash

    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        root = _root(catalog)
        uuid = catalog.post(f"/v1/table/{_TABLE}/describe", json={}).json()["properties"]["rask.uuid"]
        settings = get_settings()
        # What a delete files before it takes the reference file away, and nothing after.
        died = branch_trash.HiddenBranch(
            name=_BRANCH,
            location=str(root),
            reference=(root / "_refs" / "branches" / "team%2Fwork.json").read_text(),
            parent_branch=None,
            parent_version=1,
            identifier=None,
            tag=branch_trash.tag_of(_BRANCH),
            dropped_by="alice",
            dropped_at=datetime.now(UTC).isoformat(),
            expires_at=datetime.now(UTC).isoformat(),
        )
        branch_trash.file(settings.registry_root, settings.storage_options(), uuid, hidden=died)

        ticked = catalog_tick(str(catalog.base_url), trash_tick.tick, clock=_later)

        written = catalog.post(f"/v1/table/{_TABLE}/insert", params={"branch": _BRANCH}, content=_rows(11), headers=_ARROW)
        assert (ticked.purged, written.status_code, _branch_ids(catalog)) == (0, 200, [1, 2, 10, 11])


def test_a_deleted_branch_past_its_deadline_is_not_restored_before_it_is_deleted_for_good(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from catalog.api.dependencies import get_clock
    from catalog.main import app

    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200
        monkeypatch.setitem(app.dependency_overrides, get_clock, lambda: _later)

        restored = catalog.post(_RESTORE, json={"name": _BRANCH})

        assert (restored.status_code, restored.json()["code"]) == (409, 19)


@pytest.mark.parametrize(
    ("asked_at", "listed"),
    [
        pytest.param(datetime(2026, 11, 25, 11, 59, tzinfo=UTC), [_BRANCH], id="inside its deadline"),
        pytest.param(datetime(2026, 11, 25, 12, tzinfo=UTC), [], id="at its deadline, where a restore is refused"),
    ],
)
def test_a_tables_deleted_branches_are_listed_until_their_deadline(
    asked_at: datetime, listed: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog.api.dependencies import get_clock
    from catalog.main import app

    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        monkeypatch.setitem(app.dependency_overrides, get_clock, lambda: lambda: datetime(2026, 11, 24, 12, tzinfo=UTC))
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200
        monkeypatch.setitem(app.dependency_overrides, get_clock, lambda: lambda: asked_at)

        answered = catalog.post(f"/management/v1/table/{_TABLE}/branches/deleted/list", json={})

        kept = {"name": _BRANCH, "parent_branch": None, "parent_version": 1, "deleted_at": "2026-11-24T12:00:00Z", "expires_at": "2026-11-25T12:00:00Z"}
        assert (answered.status_code, answered.json()) == (200, {"branches": [kept for _ in listed]})


@pytest.mark.parametrize("door", ["tags/create", "tags/update", "tags/delete"])
def test_the_tag_holding_a_deleted_branchs_cut_point_is_the_catalogs_own(door: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200
        (held,) = lance.dataset(str(_root(catalog))).tags.list()

        answered = catalog.post(f"/v1/table/{_TABLE}/{door}", json={"tag": held, "version": 1})

        assert (held.startswith("rask.trash."), answered.status_code, answered.json()["code"]) == (True, 400, 13)


def test_recording_an_erasure_deletes_the_tables_deleted_branches_for_good(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200
        kept = _root(catalog) / "tree" / _BRANCH
        kept_before = kept.exists()

        recorded = catalog.post(_ERASURE, json={"predicate": "id = 10"})

        restored = catalog.post(_RESTORE, json={"name": _BRANCH})
        assert (kept_before, recorded.status_code, kept.exists(), _records(catalog), restored.status_code) == (True, 202, False, [], 409)


def _records(catalog: httpx.Client) -> list[str]:
    """The names the control root keeps a hidden-branch record for, of table ``t``."""
    from catalog.core.config import get_settings
    from lakehouse.records import branch_trash

    settings = get_settings()
    uuid = catalog.post(f"/v1/table/{_TABLE}/describe", json={}).json()["properties"]["rask.uuid"]
    return [hidden.name for hidden in branch_trash.of_table(settings.registry_root, settings.storage_options(), uuid)]


def test_a_verification_the_catalog_refuses_deletes_no_deleted_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from catalog.core.config import get_settings
    from lakehouse.records import branch_trash

    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        obligation = catalog.post(_ERASURE, json={"predicate": "id = 10"}).json()["obligation_id"]
        # A delete that decided before the erasure was recorded and hid its branch after: the record, and no reference file.
        root, settings = _root(catalog), get_settings()
        reference = root / "_refs" / "branches" / "team%2Fwork.json"
        raced = branch_trash.HiddenBranch(
            name=_BRANCH,
            location=str(root),
            reference=reference.read_text(),
            parent_branch=None,
            parent_version=1,
            identifier=None,
            tag=branch_trash.tag_of(_BRANCH),
            dropped_by="alice",
            dropped_at=datetime.now(UTC).isoformat(),
            expires_at=_later().isoformat(),
        )
        uuid = catalog.post(f"/v1/table/{_TABLE}/describe", json={}).json()["properties"]["rask.uuid"]
        branch_trash.file(settings.registry_root, settings.storage_options(), uuid, hidden=raced)
        reference.unlink()

        refused = catalog.post(f"{_ERASURE}/{obligation}/verify", json={"predicate": "id = 11"})

        assert (refused.status_code, (root / "tree" / _BRANCH).exists(), _records(catalog)) == (400, True, [_BRANCH])


def _an_open_obligation(catalog: httpx.Client) -> None:
    assert catalog.post(_ERASURE, json={"predicate": "id = 10"}).status_code == 202


def _a_scheduled_release(catalog: httpx.Client) -> None:
    from catalog.core.config import get_settings
    from catalog.services import erasure_release

    settings = get_settings()
    uuid = catalog.post(f"/v1/table/{_TABLE}/describe", json={}).json()["properties"]["rask.uuid"]
    release = erasure_release.new_release(
        [], release_id="r1", table=_TABLE, table_uuid=uuid, location=str(_root(catalog)), requested_by=None, notified=[], due_at=_later()
    )
    erasure_release.schedule(settings.registry_root, settings.storage_options(), release)


@pytest.mark.parametrize("in_flight", [_an_open_obligation, _a_scheduled_release])
def test_a_branch_deleted_while_an_erasure_is_in_flight_is_destroyed_at_once(
    in_flight: Callable[[httpx.Client], None], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        kept = _root(catalog) / "tree" / _BRANCH
        in_flight(catalog)

        deleted = catalog.post(_DELETE, json={"name": _BRANCH})

        assert (deleted.status_code, kept.exists()) == (200, False)


def test_a_deregistered_table_keeps_no_deleted_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _a_table_with_a_branch_holding_a_commit(tmp_path, monkeypatch) as catalog:
        assert catalog.post(_DELETE, json={"name": _BRANCH}).status_code == 200
        root = _root(catalog)

        deregistered = catalog.post(f"/v1/table/{_TABLE}/deregister", json={})

        assert (deregistered.status_code, (root / "tree" / _BRANCH).exists(), list(lance.dataset(str(root)).tags.list())) == (200, False, [])
