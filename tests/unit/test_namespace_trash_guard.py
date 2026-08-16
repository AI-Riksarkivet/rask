"""A namespace id whose previous occupant is still recoverable is not a free name.

F10 item 4 closed this on the TABLE doors. `require_no_live_trash` already spoke `kind="namespace"` —
it picks `NamespaceAlreadyExistsError` over `TableAlreadyExistsError` on exactly that argument — and
neither namespace create door ever called it. The guard was written for this case and wired to
nothing.

THE BLEED. A recoverable drop deliberately KEEPS the object's FGA tuples: revoking them made `undrop`
unreachable for the owner, who is the one caller that needs it. Correct for undrop, a hole for create.
So a create at the same id during the grace window returns a brand-new namespace silently wearing the
dead one's readers, writers and validators — the stale-grant bleed `revoke_object_tuples` exists to
prevent, arriving through the door that opted out of it.

AND FOR A NAMESPACE IT IS WORSE THAN FOR A TABLE, which is why fixing only the table doors was not
"most of it". The #96 recoverable cascade trashes a whole SUBTREE and detaches it; `undrop` then walks
that subtree, recreates each namespace with `mode="exist_ok"` — so it ADOPTS whatever now stands at
the id rather than refusing — and re-registers the trashed tables underneath. Take a trashed namespace
name during the grace window and the previous tenant's tables are later registered into the namespace
you now own. Not a leaked grant: a delivery of someone else's data.

Refusing is the honest answer rather than revoking-then-creating. The bytes are still there and their
owner still holds a live claim to recover them, so a create that quietly took the name would trade a
privilege bleed for silent data loss.

Real `file://` control root and the real `service_kit.lakehouse.trash` primitives, per the house
standard in `test_trash_purge.py`: every refusal here is a claim about what is REALLY on the control
store, and a double would happily agree with a wrong one.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from catalog.api import fga_deps
from catalog.api.v1.endpoints import namespaces as ns_endpoint
from catalog.api.v1.endpoints import warehouses as wh_endpoint
from catalog.core.config import Settings
from lance_namespace import NamespaceAlreadyExistsError, TableAlreadyExistsError

from service_kit.lakehouse import trash


REPO_ROOT = Path(__file__).resolve().parents[2]
_DEADLINE = (datetime(2026, 8, 16, tzinfo=UTC) + timedelta(days=7)).isoformat()


def _settings(tmp_path: Path) -> Settings:
    """A real control root on the local filesystem. The S3 creds are required fields that
    `storage_options()` passes through — stubbed the way the live catalog boot supplies them; the
    `file://` root never reads them."""
    return Settings(
        LANCE_CONTROL_ROOT=f"file://{tmp_path / 'control'}",
        LANCE_S3_ACCESS_KEY_ID="unit",
        LANCE_S3_SECRET_ACCESS_KEY="unit",
    )


def _trash_a_namespace(settings: Settings, canonical: str, *, kind: str = "namespace", **over: Any) -> None:
    record: dict[str, Any] = {"id": canonical, "kind": kind, "expires_at": _DEADLINE, **over}
    trash.put(settings.registry_root, settings.storage_options(), record)


# --------------------------------------------------------------------------- #
# the guard, with kind="namespace"
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_free_namespace_id_passes(tmp_path: Path) -> None:
    """The guard must be invisible in the ordinary case — nothing in the trash, nothing refused."""
    settings = _settings(tmp_path)
    await fga_deps.require_no_live_trash(settings, ["acme", "bronze"], kind="namespace")


@pytest.mark.anyio
async def test_an_id_still_in_the_trash_is_REFUSED(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _trash_a_namespace(settings, "acme$bronze")
    with pytest.raises(NamespaceAlreadyExistsError):
        await fga_deps.require_no_live_trash(settings, ["acme", "bronze"], kind="namespace")


@pytest.mark.anyio
async def test_the_refusal_is_the_NAMESPACE_conflict_not_the_table_one(tmp_path: Path) -> None:
    """Both map to 409, and a generated client dispatches on the spec's numeric CODE, not the status.

    A namespace create that failed as `TableAlreadyExists` would send a client's error handling down
    its table-conflict branch for an object that is not a table.
    """
    settings = _settings(tmp_path)
    _trash_a_namespace(settings, "acme$bronze")
    with pytest.raises(NamespaceAlreadyExistsError) as exc:
        await fga_deps.require_no_live_trash(settings, ["acme", "bronze"], kind="namespace")
    assert not isinstance(exc.value, TableAlreadyExistsError)


@pytest.mark.anyio
async def test_the_refusal_names_the_deadline_and_BOTH_ways_out(tmp_path: Path) -> None:
    """ "Already exists" about an object the caller cannot see is unactionable.

    They can see no such namespace in any listing — it was detached — so the message has to carry the
    whole situation: that it is recoverable, until when, and the two operations that free the name.
    """
    settings = _settings(tmp_path)
    _trash_a_namespace(settings, "acme$bronze")
    with pytest.raises(NamespaceAlreadyExistsError) as exc:
        await fga_deps.require_no_live_trash(settings, ["acme", "bronze"], kind="namespace")
    detail = str(exc.value)
    assert "acme$bronze" in detail
    assert _DEADLINE in detail, "must name the deadline, or the caller cannot tell whether to wait"
    assert "undrop" in detail and "purge" in detail, "must name both ways out"
    assert "namespace" in detail and "table" not in detail, "must describe the object the caller actually asked for"


@pytest.mark.anyio
async def test_a_record_with_no_deadline_still_refuses_and_says_so(tmp_path: Path) -> None:
    """A malformed record is still a live claim on the name. Refusing without a date is right; refusing
    with an empty date where a date should be would read as a bug in the message."""
    settings = _settings(tmp_path)
    trash.put(settings.registry_root, settings.storage_options(), {"id": "acme$bronze", "kind": "namespace"})
    with pytest.raises(NamespaceAlreadyExistsError) as exc:
        await fga_deps.require_no_live_trash(settings, ["acme", "bronze"], kind="namespace")
    assert "unrecorded deadline" in str(exc.value)


@pytest.mark.anyio
async def test_a_trashed_TABLE_does_not_block_a_NAMESPACE_of_the_same_name(tmp_path: Path) -> None:
    """Trash records are keyed by (id, kind), and the guard must respect that in both directions.

    A guard that ignored `kind` would refuse a legitimate namespace create because an unrelated table
    once had the same name — turning a safety check into an arbitrary name ban.
    """
    settings = _settings(tmp_path)
    _trash_a_namespace(settings, "acme$bronze", kind="table")
    await fga_deps.require_no_live_trash(settings, ["acme", "bronze"], kind="namespace")
    # …and the converse, so the isolation is not accidentally one-directional.
    with pytest.raises(TableAlreadyExistsError):
        await fga_deps.require_no_live_trash(settings, ["acme", "bronze"], kind="table")


@pytest.mark.anyio
async def test_an_unreadable_trash_store_does_NOT_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliberate, and worth a test because it is the opposite of the reflex everywhere else here.

    Fail-closed is right for AUTHZ — a check that cannot answer must not grant. This is not an authz
    check: it closes a narrow, time-boxed window, and making it fail closed would let one unreadable
    object on the control store take down namespace creation for the entire estate. Trading a
    guaranteed outage for a rare bleed is the wrong side of that bargain. The event is logged.
    """
    settings = _settings(tmp_path)

    def _explode(*_a: object, **_k: object) -> None:
        raise OSError("control store unreachable")

    monkeypatch.setattr(trash, "get", _explode)
    await fga_deps.require_no_live_trash(settings, ["acme", "bronze"], kind="namespace")


# --------------------------------------------------------------------------- #
# both doors
# --------------------------------------------------------------------------- #


def _door_body(module: ModuleType, name: str) -> str:
    source = Path(module.__file__ or "").read_text()
    return source.split(f"def {name}(", 1)[1].split("\n@router", 1)[0]


@pytest.mark.parametrize(
    ("module", "door"),
    [(ns_endpoint, "create_namespace"), (wh_endpoint, "create_warehouse_namespace")],
    ids=["nested", "warehouse-scoped"],
)
def test_BOTH_namespace_create_doors_call_the_guard(module: ModuleType, door: str) -> None:
    """Fixing one door and not the other leaves the hazard reachable by the other route — and the
    warehouse-scoped door is the one that creates the ROOT of a cascade-trashed subtree, which is
    precisely the id `undrop` walks from."""
    body = _door_body(module, door)
    assert "require_no_live_trash" in body, f"{door} does not check the trash"


@pytest.mark.parametrize(
    ("module", "door"),
    [(ns_endpoint, "create_namespace"), (wh_endpoint, "create_warehouse_namespace")],
    ids=["nested", "warehouse-scoped"],
)
def test_both_doors_check_the_trash_as_a_NAMESPACE(module: ModuleType, door: str) -> None:
    """`kind` defaults to `"table"`, so omitting it here would probe the wrong record entirely: the
    guard would read the table trash, find nothing, and pass — a check that runs and proves nothing."""
    body = _door_body(module, door)
    call = re.search(r"require_no_live_trash\((.*?)\)", body, re.DOTALL)
    assert call, f"{door} has no require_no_live_trash call to inspect"
    assert 'kind="namespace"' in call.group(1), f"{door} probes the trash as a table, not a namespace"


@pytest.mark.parametrize(
    ("module", "door"),
    [(ns_endpoint, "create_namespace"), (wh_endpoint, "create_warehouse_namespace")],
    ids=["nested", "warehouse-scoped"],
)
def test_the_trash_check_runs_BEFORE_the_native_create(module: ModuleType, door: str) -> None:
    """Check order: identity -> shape -> parent exists -> authz -> conflict -> native write. A conflict
    found after the write has already created a real Lance object at the id it was refusing."""
    body = _door_body(module, door)
    assert body.index("require_no_live_trash") < body.index('"create_namespace"'), f"{door} checks the trash too late"


def test_the_guard_still_defaults_to_table_for_the_table_doors() -> None:
    """The table doors call it positionally and rely on the default; changing it to satisfy the
    namespace doors would silently repoint every table create at the namespace trash."""
    source = (REPO_ROOT / "services/catalog/src/catalog/api/fga_deps.py").read_text()
    assert re.search(r'def require_no_live_trash\([^)]*kind: str = "table"', source, re.DOTALL)
