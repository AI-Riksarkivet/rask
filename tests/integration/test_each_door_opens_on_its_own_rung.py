"""Each catalog door opens on its own rung and no lower, on the object its name resolves to ([[LH-235]]).

The model owns the op -> privilege mapping: a door asks a ``can_*`` action relation, and each reduces to a rung of the
model (`packages/lakehouse/src/lakehouse/governed/auth/model.fga`). Reads open on ``reader``, a data write on
``writer``, and what destroys, rewinds, renames, re-points a ref, sets a policy or reviews who holds access on
``owner``. A create, declare or materialized view is judged on its PARENT namespace, which exists, never on the child
it makes. A project's policy doors open on the project's ``admin``. The batch doors judge every table their body names,
a destructive sub-operation at its own door's owner rung, and answer the spec's 406 past the gate.

Each door is driven as a pair against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA
deciding on a store of the test's own: alice granted exactly the door's rung is answered what the door answers, and
alice granted the rung just below it is refused 403 (code 15), with the store's tuples and the table's version as they
were. bob owns every object, as the doors that made them seed him.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

import lance_namespace
import pytest
from pydantic import BaseModel, ConfigDict

from catalog.api.v1.endpoints.access import _can_relations as _model_can_relations
from catalog.core.config import get_settings
from catalog.services import warehouses
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE
from lakehouse.records.warehouse_records import WarehouseRecord
from tests.governed_catalog import ROWS, GovernedCatalog


#: The Lance Namespace spec's PermissionDenied (`lance_docs/ns_catalog/spec.yaml`).
PERMISSION_DENIED = 15
#: The spec's InvalidInput, a request malformed whatever its caller holds.
INVALID_INPUT = 13
#: The estate's objects: bob's namespace, the table in it, an empty namespace in it, tables whose names hold a `#` and a `?`.
NS, TABLE, SUB, HASHED = "lance_catalog$db1", "lance_catalog$db1$users", "lance_catalog$db1$sub", "lance_catalog$db1$us#ers"
QUESTIONED = "lance_catalog$db1$us?ers"
OTHER = "lance_catalog$db1$b"
PROJECT = "project:acme"
VIEW = "materialized_view:lance_catalog$db1$mv"


class Estate(BaseModel):
    """The catalog and the FGA object each of bob's names resolves to."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    catalog: GovernedCatalog
    objects: dict[str, str]

    def grant(self, rung: str | None, *targets: str) -> None:
        """Grant alice ``rung`` on each of ``targets``, a name of :data:`objects` or an object id; nothing for ``None``."""
        if rung is not None:
            self.catalog.store.write(*((self.catalog.person("alice"), rung, self.objects.get(t, t)) for t in targets))


@pytest.fixture
def estate(governed_catalog: GovernedCatalog) -> Estate:
    """bob's ``db1`` holding ``users``, ``b``, ``us#ers``, ``us?ers`` and the empty namespace ``sub``, made through the doors."""
    catalog = governed_catalog
    assert catalog.create_in_the_estate("bob", "db1").status_code == 200
    for table in (TABLE, OTHER, HASHED, QUESTIONED):
        catalog.create_table("bob", table)
    catalog.ok(f"/v1/namespace/{SUB}/create", "bob", json={})
    objects = {NS: f"namespace:{catalog.uuid('namespace', NS, 'bob')}", SUB: f"namespace:{catalog.uuid('namespace', SUB, 'bob')}"}
    objects |= {t: f"table:{catalog.uuid('table', t, 'bob')}" for t in (TABLE, OTHER, HASHED, QUESTIONED)}
    return Estate(catalog=catalog, objects=objects)


def _recoverably(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep what a drop removes in the trash for a week, so an undrop has something to restore."""
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    get_settings.cache_clear()


def _drop_table(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    _recoverably(monkeypatch)
    estate.catalog.ok(f"/v1/table/{TABLE}/drop", "bob", json={})


def _drop_sub(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    _recoverably(monkeypatch)
    estate.catalog.ok(f"/v1/namespace/{SUB}/drop", "bob", json={"behavior": "Cascade"})


def _tag(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    created = estate.catalog.client.post(f"/v1/table/{TABLE}/tags/create", json={"tag": "v1", "version": 1}, headers=estate.catalog.bearer("bob"))
    assert created.status_code in (200, 201), created.text


def _project(estate: Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """bob, the estate's admin as its declaration grants him, makes the project ``acme`` through its door, holding the
    warehouse ``wh-acme``: its record, its row in its root's catalog and its project edge are written where the
    warehouse door writes them, since that door provisions a bucket a ``dir`` estate has none of."""
    del monkeypatch
    estate.catalog.store.write((estate.catalog.person("bob"), "admin", "estate:rask"))
    made = estate.catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=estate.catalog.bearer("bob"))
    assert made.status_code in (200, 201), made.text
    settings = get_settings()
    record = {
        "id": "wh-acme",
        "bucket": "wh-acme",
        "root_uri": settings.root,
        "project": "acme",
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }
    warehouses.create_warehouse_record(settings.registry_root, settings.storage_options(), WarehouseRecord.model_validate(record))
    warehouses.ensure_catalog_row(lance_namespace.connect("dir", {"root": settings.root}), "wh-acme")
    estate.catalog.store.write((PROJECT, "project", "warehouse:wh-acme"))


class Door(BaseModel):
    """A request, the rung it opens on and the rung just below, the objects both are granted on, and what it answers."""

    model_config = ConfigDict(frozen=True)

    method: str = "post"
    path: str
    body: dict[str, Any] | None = None
    params: dict[str, str | int] | None = None
    arrow: bool = False
    rung: str
    below: str | None
    on: tuple[str, ...] = (TABLE,)
    also: tuple[tuple[str, str], ...] = ()
    answers: int = 200
    setup: str | None = None


_SETUPS: Final[frozendict[str, Callable[[Estate, pytest.MonkeyPatch], None]]] = frozendict(
    {"drop_table": _drop_table, "drop_sub": _drop_sub, "tag": _tag, "project": _project}
)

_DOORS: Final = frozendict(
    {
        "describe-is-a-reader-read": Door(path=f"/v1/table/{TABLE}/describe", body={"vend_credentials": False}, rung="reader", below=None),
        # Past the gate, the blob door refuses a column that holds no blobs: the answer of a reader, not of a refused caller.
        "a-blob-read-is-a-data-read": Door(
            method="get", path=f"/management/v1/table/{TABLE}/blobs", params={"column": "id", "row": 0}, rung="reader", below=None, answers=400
        ),
        "update-is-a-writer-write": Door(
            path=f"/v1/table/{TABLE}/update", body={"predicate": "id = 1", "updates": [["id", "3"]]}, rung="writer", below="reader"
        ),
        "rename-is-the-owners": Door(path=f"/v1/table/{TABLE}/rename", body={"new_table_name": "u2"}, rung="owner", below="writer", also=(("writer", NS),)),
        "drop-is-the-owners": Door(path=f"/v1/table/{TABLE}/drop", body={}, rung="owner", below="writer"),
        "deregister-is-the-owners": Door(path=f"/v1/table/{TABLE}/deregister", body={}, rung="owner", below="writer"),
        "restore-is-the-owners": Door(path=f"/v1/table/{TABLE}/restore", body={"version": 1}, rung="owner", below="writer"),
        "a-branch-is-the-owners": Door(path=f"/v1/table/{TABLE}/branches/create", body={"name": "exp"}, rung="owner", below="writer"),
        # A tag is a named pointer, so its owner or a tagger moves it (`can_create_tag: owner_effective or tagger_effective`):
        # the tagger rung, which nothing below the owner implies, opens it and a writer is refused.
        "a-tag-is-a-taggers": Door(path=f"/v1/table/{TABLE}/tags/create", body={"tag": "v1", "version": 1}, rung="tagger", below="writer"),
        "a-tag-moved-is-a-taggers": Door(path=f"/v1/table/{TABLE}/tags/update", body={"tag": "v1", "version": 1}, rung="tagger", below="writer", setup="tag"),
        "an-encoded-hash-keeps-the-drop-the-owners": Door(
            path="/v1/table/lance_catalog$db1$us%23ers/drop", body={}, rung="owner", below="writer", on=(HASHED,)
        ),
        "an-encoded-hash-keeps-the-deregister-the-owners": Door(
            path="/v1/table/lance_catalog$db1$us%23ers/deregister", body={}, rung="owner", below="writer", on=(HASHED,)
        ),
        "an-encoded-question-mark-keeps-the-drop-the-owners": Door(
            path="/v1/table/lance_catalog$db1$us%3Fers/drop", body={}, rung="owner", below="writer", on=(QUESTIONED,)
        ),
        "an-encoded-question-mark-keeps-the-deregister-the-owners": Door(
            path="/v1/table/lance_catalog$db1$us%3Fers/deregister", body={}, rung="owner", below="writer", on=(QUESTIONED,)
        ),
        "a-table-policy-is-the-owners": Door(path=f"/management/v1/table/{TABLE}/policy/set", body={"retention_days": 7}, rung="owner", below="writer"),
        "a-table-policy-is-read-by-a-reader": Door(path=f"/management/v1/table/{TABLE}/policy/describe", rung="reader", below=None),
        "a-table-review-is-the-owners": Door(path=f"/management/v1/table/{TABLE}/access/list", rung="owner", below="writer"),
        "a-table-deadline-is-a-metadata-read": Door(method="get", path=f"/management/v1/table/{TABLE}/tasks", rung="reader", below=None),
        "a-table-undrop-is-the-drops-rung": Door(path=f"/management/v1/table/{TABLE}/undrop", rung="owner", below="writer", setup="drop_table"),
        "a-namespace-drop-is-the-owners": Door(path=f"/v1/namespace/{SUB}/drop", body={}, rung="owner", below="writer", on=(SUB,)),
        "a-namespace-policy-is-the-owners": Door(
            path=f"/management/v1/namespace/{SUB}/policy/set", body={"retain_versions": 5}, rung="owner", below="writer", on=(SUB,)
        ),
        "a-namespace-review-is-the-owners": Door(path=f"/management/v1/namespace/{SUB}/access/list", rung="owner", below="writer", on=(SUB,)),
        "a-namespace-deadline-is-a-metadata-read": Door(method="get", path=f"/management/v1/namespace/{SUB}/tasks", rung="reader", below=None, on=(SUB,)),
        "a-namespace-undrop-is-the-drops-rung": Door(path=f"/management/v1/namespace/{SUB}/undrop", rung="owner", below="writer", on=(SUB,), setup="drop_sub"),
        "a-create-is-judged-on-its-parent": Door(path="/v1/table/lance_catalog$db1$fresh/create", arrow=True, rung="writer", below="reader", on=(NS,)),
        "a-declare-is-judged-on-its-parent": Door(path="/v1/table/lance_catalog$db1$fresh/declare", body={}, rung="writer", below="reader", on=(NS,)),
        "an-overwrite-of-a-standing-table-is-its-owners": Door(
            path=f"/v1/table/{TABLE}/create", params={"mode": "overwrite"}, arrow=True, rung="owner", below="writer", also=(("writer", NS),)
        ),
        "a-view-is-judged-on-its-parent": Door(
            path="/v1/materialized_view/lance_catalog$db1$mv/create",
            body={"kind": "query", "source_query": "select 1", "output_schema": "{}"},
            rung="writer",
            below="reader",
            on=(NS,),
            answers=406,
        ),
        "a-view-refresh-is-its-refreshers": Door(
            path="/v1/materialized_view/lance_catalog$db1$mv/refresh", rung="refresher", below="reader", on=(VIEW,), answers=406
        ),
        "a-batch-commit-judges-every-table-it-names": Door(
            path="/v1/table/batch-commit",
            body={
                "operations": [{"create_table_version": {"id": t.split("$"), "version": 2, "manifest_path": "_versions/2.manifest"}} for t in (TABLE, OTHER)]
            },
            rung="writer",
            below="reader",
            on=(OTHER,),
            also=(("writer", TABLE),),
            answers=406,
        ),
        "a-batch-deregister-is-its-doors-owner-rung": Door(
            path="/v1/table/batch-commit", body={"operations": [{"deregister_table": {"id": TABLE.split("$")}}]}, rung="owner", below="writer", answers=406
        ),
        "a-batch-of-versions-is-a-writer-write": Door(
            path="/v1/table/version/batch-create",
            body={"entries": [{"id": TABLE.split("$"), "version": 2, "manifest_path": "_versions/2.manifest"}]},
            rung="writer",
            below="reader",
            answers=406,
        ),
        "a-project-policy-is-its-admins": Door(
            path="/management/v1/project/acme/policy/set", body={"retention_days": 90}, rung="admin", below="member", on=(PROJECT,), setup="project"
        ),
        "a-project-policy-is-read-by-its-admins": Door(
            path="/management/v1/project/acme/policy/describe", rung="admin", below="member", on=(PROJECT,), setup="project"
        ),
        "a-project-policy-is-ended-by-its-admins": Door(
            path="/management/v1/project/acme/policy/delete", rung="admin", below="member", on=(PROJECT,), setup="project"
        ),
        "a-projects-policies-are-listed-to-its-admins": Door(
            method="get", path="/management/v1/projects/acme/policies", rung="admin", below="member", on=(PROJECT,), setup="project"
        ),
    }
)


def _send(estate: Estate, door: Door) -> Any:  # noqa: ANN401 — the door's httpx response
    client, headers = estate.catalog.client, estate.catalog.bearer("alice")
    if door.arrow:
        return client.post(door.path, content=ROWS, params=door.params, headers={**headers, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    if door.method == "get":
        return client.get(door.path, params=door.params, headers=headers)
    return client.post(door.path, json=door.body, params=door.params, headers=headers)


def _state(estate: Estate) -> tuple[set[tuple[str, str, str]], Any]:
    """What a refused door must leave as it was: the store's tuples, and the table as bob describes it."""
    described = estate.catalog.client.post(f"/v1/table/{TABLE}/describe", json={}, headers=estate.catalog.bearer("bob"))
    return estate.catalog.store.tuples(), (described.status_code, described.json().get("version"))


@pytest.mark.parametrize("granted", ["rung", "below"])
@pytest.mark.parametrize("door", [pytest.param(door, id=name) for name, door in _DOORS.items()])
def test_a_door_opens_on_its_own_rung_and_refuses_the_rung_below_leaving_everything_as_it_was(
    estate: Estate, door: Door, granted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if door.setup is not None:
        _SETUPS[door.setup](estate, monkeypatch)
    for rung, target in door.also:
        estate.grant(rung, target)
    if granted == "rung":
        estate.grant(door.rung, *door.on)
        answered = _send(estate, door)
        assert answered.status_code == door.answers, answered.text
        return
    estate.grant(door.below, *door.on)
    before = _state(estate)

    answered = _send(estate, door)

    assert (answered.status_code, answered.json().get("code")) == (403, PERMISSION_DENIED), answered.text
    assert answered.headers["content-type"].startswith("application/problem+json")
    assert _state(estate) == before, "a refused door changed a grant or the table"


def test_the_estate_listing_shows_only_the_tables_its_caller_can_read(estate: Estate) -> None:
    """``GET /v1/table`` judges its own candidates, each on the table its name resolves to ([[LH-223]])."""
    estate.grant("reader", TABLE, OTHER)

    listed = estate.catalog.client.get("/v1/table", headers=estate.catalog.bearer("alice"))

    assert listed.status_code == 200, listed.text
    assert listed.json()["tables"] == sorted([OTHER, TABLE])


def test_an_owners_review_lists_who_holds_each_action(estate: Estate) -> None:
    """The review expands every ``can_*`` action of the type to the people it reaches, from the store's own answers."""
    estate.grant("owner", TABLE)
    estate.catalog.store.write((estate.catalog.person("carol"), "reader", estate.objects[TABLE]))

    answered = estate.catalog.client.post(f"/management/v1/table/{TABLE}/access/list", headers=estate.catalog.bearer("alice"))

    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert (body["object"], body["name"]) == (estate.objects[TABLE], TABLE)
    grants = {g["relation"]: sorted(g["users"]) for g in body["grants"]}
    assert set(grants) == set(_model_can_relations("table"))
    assert (grants["can_get_metadata"], grants["can_drop"]) == (["dex~alice", "dex~bob", "dex~carol"], ["dex~alice", "dex~bob"])
    assert (body["truncated"], [g["relation"] for g in body["grants"] if g["truncated"]]) == (False, []), "a complete review was flagged truncated"


def test_a_create_the_backend_refuses_seeds_no_grant(estate: Estate) -> None:
    """A create over a table that stands is a conflict (409), and the refused create leaves every tuple as it was."""
    estate.grant("writer", NS)
    before = _state(estate)

    answered = estate.catalog.client.post(
        f"/v1/table/{TABLE}/create",
        content=ROWS,
        params={"mode": "create"},
        headers={**estate.catalog.bearer("alice"), "Content-Type": ARROW_STREAM_MEDIA_TYPE},
    )

    assert answered.status_code == 409, answered.text
    assert _state(estate) == before, "a refused create seeded or ended a grant"


def test_a_batch_body_too_malformed_to_judge_is_refused_whatever_its_caller_holds(estate: Estate) -> None:
    """``operations`` that is no list names no table to judge, so it is the caller's malformed request (400, before any
    permission is asked, ADR 0173 §4) rather than passed or crashed on, to a caller who writes every table the catalog
    holds."""
    estate.grant("writer", TABLE, OTHER, HASHED, QUESTIONED)

    answered = estate.catalog.client.post("/v1/table/batch-commit", json={"operations": 42}, headers=estate.catalog.bearer("alice"))

    assert (answered.status_code, answered.json()["code"]) == (400, INVALID_INPUT), answered.text


@pytest.mark.parametrize(
    ("door", "destroyed"),
    [
        pytest.param(f"/v1/table/{TABLE}/drop", TABLE, id="a-drop"),
        pytest.param(f"/v1/table/{TABLE}/deregister", TABLE, id="a-deregister"),
        pytest.param(f"/v1/namespace/{SUB}/drop", SUB, id="a-restrict-namespace-drop"),
    ],
)
def test_a_destructive_door_ends_the_grants_of_what_it_destroyed_and_of_nothing_else(estate: Estate, door: str, destroyed: str) -> None:
    """The object's own tuples go, and every other object keeps its own: a Restrict drop ends no child's or sibling's."""
    obj = estate.objects[destroyed]
    before = estate.catalog.store.tuples()
    assert any(obj in (t[0], t[2]) for t in before), "the object held no tuple, so its revoke would pass vacuously"

    estate.catalog.ok(door, "bob", json={})

    after = estate.catalog.store.tuples()
    assert {t for t in after if obj in (t[0], t[2])} == set(), "a grant or an edge outlived the object it was on"
    assert {t for t in before if obj not in (t[0], t[2])} <= after, "a destructive door ended a grant on another object"
