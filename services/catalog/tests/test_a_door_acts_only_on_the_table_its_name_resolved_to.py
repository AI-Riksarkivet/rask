"""A door acts on the table its name resolved to, so a drop and a re-create racing it reach no other table ([[LH-530]]).

The router authorizes a door on the uuid its name resolves to (ADR 0173 §8, [[LH-524]]) and the door acts on the location
that resolution read (ADR 0173 §7). A door that called the backend by name again would act on whatever table the name
names by then: bob, granted `owner` on alice's `db$t`, would read or change the table alice created under that name
after dropping his, which grants him nothing.

So the rival lands exactly there, on every door that reads or writes a table's data or metadata: alice drops `db$t` and
creates it again once the router has authorized bob's request, and before the door acts. The door answers 409 code 14,
and the table alice created is left as she made it. Two rivals leave a table at the location the door resolved, and each
is driven on a door acting through a handle and on a native read. A recoverable drop keeps the dropped table's bytes and
its claim, unchanged, there, so a door that opened that location and judged its claim alone would act on a table in the
trash. A deregister and a register at the same location leave the name resolving there, to a table of another uuid, so
a door that judged the location alone would act on it. A write Lance serves only through the namespace names the table
again as it is made, so a drop and a create also land as the door makes one, after every open it made. A route mounted
for both POST and GET is one handler, so it is driven once.

Every decision is the real model's, on a store of this test's own ([[LH-235]]); every tuple is the one the door writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, ResolverDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.services import table_claims
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_ARROW = {"Content-Type": ARROW_STREAM_MEDIA_TYPE}
_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64()), "v": pa.array([10, 20], pa.int64())}))
_RIVAL_ROWS = encode_arrow_stream(pa.table({"id": pa.array([9], pa.int64()), "v": pa.array([90], pa.int64())}))
_QUERY = {"k": 10, "vector": {"single_vector": []}}
_TABLE = "/v1/table/db$t"
_MANAGED = "/management/v1/table/db$t"

#: Every door that reads or writes a table's data or metadata, as bob sends it: (method, path, keywords).
_DOORS = [
    pytest.param("POST", f"{_TABLE}/describe", {"json": {}}, id="describe"),
    pytest.param("POST", f"{_TABLE}/exists", {"json": {}}, id="exists"),
    pytest.param("POST", f"{_TABLE}/query", {"json": _QUERY}, id="query"),
    pytest.param("POST", f"{_TABLE}/count_rows", {"json": {}}, id="count-rows"),
    pytest.param("POST", f"{_TABLE}/explain_plan", {"json": {"query": _QUERY}}, id="explain-plan"),
    pytest.param("POST", f"{_TABLE}/analyze_plan", {"json": _QUERY}, id="analyze-plan"),
    pytest.param("POST", f"{_TABLE}/stats", {"json": {}}, id="stats"),
    pytest.param("POST", f"{_TABLE}/version/list", {"json": {}}, id="list-versions"),
    pytest.param("POST", f"{_TABLE}/version/describe", {"json": {"version": 1}}, id="describe-version"),
    pytest.param("POST", f"{_TABLE}/tags/list", {"json": {}}, id="list-tags"),
    pytest.param("POST", f"{_TABLE}/tags/version", {"json": {"tag": "t"}}, id="tag-version"),
    pytest.param("POST", f"{_TABLE}/branches/list", {"json": {}}, id="list-branches"),
    pytest.param("POST", f"{_TABLE}/index/list", {"json": {}}, id="list-indices"),
    pytest.param("POST", f"{_TABLE}/index/v_idx/stats", {"json": {}}, id="index-stats"),
    pytest.param("GET", f"{_MANAGED}/history", {}, id="history"),
    pytest.param("GET", f"{_MANAGED}/blobs?column=v&row=0", {}, id="blobs"),
    pytest.param("POST", f"{_MANAGED}/changes", {"json": {"begin_version": 0, "end_version": 1}}, id="changes"),
    pytest.param("POST", f"{_MANAGED}/credentials", {"json": {}}, id="credentials"),
    pytest.param("POST", f"{_MANAGED}/maintenance/preview", {"json": {"retain_versions": 1}}, id="maintenance-preview"),
    pytest.param("POST", f"{_TABLE}/insert", {"content": _ROWS, "headers": _ARROW}, id="insert"),
    pytest.param("POST", f"{_TABLE}/insert?mode=overwrite", {"content": _ROWS, "headers": _ARROW}, id="insert-overwrite"),
    pytest.param(
        "POST", f"{_TABLE}/merge_insert?on=id&when_matched_update_all=true&when_not_matched_insert_all=true", {"content": _ROWS, "headers": _ARROW}, id="merge"
    ),
    pytest.param("POST", f"{_TABLE}/create?mode=overwrite", {"content": _ROWS, "headers": _ARROW}, id="create-overwrite"),
    pytest.param("POST", f"{_TABLE}/update", {"json": {"updates": [["v", "v + 1"]]}}, id="update"),
    pytest.param("POST", f"{_TABLE}/delete", {"json": {"predicate": "id = 1"}}, id="delete"),
    pytest.param("POST", f"{_TABLE}/add_columns", {"json": {"new_columns": [{"name": "w", "expression": "id * 2"}]}}, id="add-columns"),
    pytest.param("POST", f"{_TABLE}/alter_columns", {"json": {"alterations": [{"path": "v", "rename": "u"}]}}, id="alter-columns"),
    pytest.param("POST", f"{_TABLE}/drop_columns", {"json": {"columns": ["v"]}}, id="drop-columns"),
    pytest.param("POST", f"{_TABLE}/backfill_column", {"json": {"column": "v"}}, id="backfill-column"),
    pytest.param("POST", f"{_TABLE}/update_field_metadata", {"json": {"updates": [{"path": "v", "metadata": {"unit": "n"}}]}}, id="field-metadata"),
    pytest.param("POST", f"{_TABLE}/schema_metadata/update", {"json": {"metadata": {"k": "x"}}}, id="schema-metadata"),
    pytest.param("POST", f"{_TABLE}/schema_metadata/update", {"json": {"metadata": {"k": None}}}, id="schema-metadata-removal"),
    pytest.param("POST", f"{_TABLE}/tags/create", {"json": {"tag": "t", "version": 1}}, id="create-tag"),
    pytest.param("POST", f"{_TABLE}/tags/update", {"json": {"tag": "t", "version": 1}}, id="update-tag"),
    pytest.param("POST", f"{_TABLE}/tags/delete", {"json": {"tag": "t"}}, id="delete-tag"),
    pytest.param("POST", f"{_TABLE}/branches/create", {"json": {"name": "b"}}, id="create-branch"),
    pytest.param("POST", f"{_TABLE}/branches/delete", {"json": {"name": "b"}}, id="delete-branch"),
    pytest.param("POST", f"{_TABLE}/version/delete", {"json": {"ranges": [{"start_version": 1, "end_version": 2}]}}, id="delete-versions"),
    pytest.param("POST", f"{_TABLE}/version/create", {"json": {"version": 2, "manifest_path": "db/t/_versions/2.manifest-x"}}, id="create-version"),
    pytest.param("POST", f"{_TABLE}/restore", {"json": {"version": 1}}, id="restore"),
    pytest.param("POST", f"{_TABLE}/index/v_idx/drop", {"json": {}}, id="drop-index"),
    pytest.param("POST", f"{_MANAGED}/maintenance/run", {"json": {"retain_versions": 1}}, id="maintenance-run"),
    pytest.param("POST", f"{_MANAGED}/erasure", {"json": {"predicate": "id = 1"}}, id="erasure"),
    pytest.param("POST", f"{_MANAGED}/erasure/{{obligation}}/verify", {"json": {"predicate": "id = 1"}}, id="erasure-verify"),
    pytest.param("POST", f"{_MANAGED}/publish", {"json": {"version": 1, "key_column": "id"}}, id="publish"),
    pytest.param("POST", f"{_MANAGED}/promotion/approve", {"json": {"version": 1}}, id="approve-promotion"),
    pytest.param("POST", f"{_MANAGED}/promotion/reject", {"json": {"version": 1}}, id="reject-promotion"),
]

#: The rivals beside a destroying drop and a create landing after authorization, which every door is driven against: two
#: that leave a table at the location the door resolved, each on a door acting through a handle there and on a native
#: read, and a drop and a create landing as the door makes its native write, on one door making one.
_RIVALS = {"recoverable-drop": {"insert", "query"}, "re-register": {"insert", "list-versions"}, "drop-at-the-native-write": {"drop-index"}}

#: Where the estate registers `db$t` for the re-register rival, beneath the namespace root.
_HELD = "held"

#: The create door authorizes the parent namespace, and an Overwrite resolves the table it overwrites and asks `can_drop`
#: on it inside the door, so the rival lands after that question; alice sends it, holding the parent's rung.
_GATED_INSIDE_THE_DOOR = {"create-overwrite"}


def _ok(catalog: Any, path: str, caller: str = "alice", status: int = 200, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 — the conftest's GovernedCatalog and the request's keywords
    answer = catalog.client.post(path, headers={**catalog.bearer(caller), **kwargs.pop("headers", {})}, **kwargs)
    assert answer.status_code == status, f"{path}: {answer.status_code} {answer.text}"
    return answer.json()


def _as_alice_made_it(catalog: Any) -> dict[str, Any]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """What alice reads of the table `db$t` names: its version, schema and metadata, its tags, its branches and its rows."""
    described = _ok(catalog, f"{_TABLE}/describe", json={"load_detailed_metadata": True, "vend_credentials": False})
    return {
        "described": {key: described.get(key) for key in ("version", "schema", "metadata", "location")},
        "tags": _ok(catalog, f"{_TABLE}/tags/list", json={}),
        "branches": _ok(catalog, f"{_TABLE}/branches/list", json={}),
        "rows": catalog.client.post(f"{_TABLE}/count_rows", json={}, headers=catalog.bearer("alice")).json(),
    }


@pytest.fixture
def estate(governed_catalog: Any, tmp_path: Path, rival: str) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's `db$t`, holding the tag `t`, the branch `b` and the index `v_idx`, which bob holds `owner` on.

    The table is registered at `held` for the re-register rival, as a register cannot name the uuid directory a create
    lays its table out in, and created otherwise. The index is a bring-your-own build, committed with the table's own
    pylance, as the catalog builds none (ADR 0162).
    """
    _ok(governed_catalog, "/v1/namespace/db/create", json={})
    if rival == "re-register":
        location = str(tmp_path / _HELD)
        lance.write_dataset(pa.ipc.open_stream(_ROWS).read_all(), location, data_storage_version="2.2", enable_stable_row_ids=True)
        _ok(governed_catalog, f"{_TABLE}/register", json={"location": _HELD})
    else:
        location = str(_ok(governed_catalog, f"{_TABLE}/create", content=_ROWS, headers=_ARROW)["location"])
    _ok(governed_catalog, f"{_TABLE}/tags/create", json={"tag": "t", "version": 1})
    _ok(governed_catalog, f"{_TABLE}/branches/create", json={"name": "b"})
    lance.dataset(location).create_scalar_index("v", index_type="BTREE", name="v_idx")
    _ok(governed_catalog, f"{_MANAGED}/access/grant", json={"user": "dex~bob", "relation": "owner"})
    return governed_catalog


@pytest.mark.parametrize(
    ("door", "method", "path", "sent", "rival"),
    [pytest.param(door.id, *door.values, "drop", id=str(door.id)) for door in _DOORS]
    + [pytest.param(door.id, *door.values, rival, id=f"{door.id}-after-a-{rival}") for rival, doors in _RIVALS.items() for door in _DOORS if door.id in doors],
)
def test_a_door_raced_by_a_drop_and_a_re_create_acts_on_no_other_table(
    estate: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    monkeypatch: pytest.MonkeyPatch,
    door: str,
    method: str,
    path: str,
    sent: dict[str, Any],
    rival: str,
) -> None:
    # The app module builds its settings at import, so it is imported once the fixture has pinned the environment.
    from catalog.core.config import get_settings
    from catalog.main import app

    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7" if rival == "recoverable-drop" else "0")
    get_settings.cache_clear()
    obligation = _ok(estate, f"{_MANAGED}/erasure", caller="bob", status=202, json={"predicate": "id = 1"})["obligation_id"]
    pending = [True]
    made: list[dict[str, Any]] = []

    def _drop_and_create_again() -> None:
        # One shot, disarmed before it sends anything: alice's own requests are authorized through here too.
        if not pending:
            return
        pending.clear()
        if rival == "re-register":
            _ok(estate, f"{_TABLE}/deregister", json={})
            _ok(estate, f"{_TABLE}/register", json={"location": _HELD})
        else:
            _ok(estate, f"{_TABLE}/drop", json={})
            _ok(estate, f"{_TABLE}/create", content=_RIVAL_ROWS, headers=_ARROW)
        made.append(_as_alice_made_it(estate))

    if rival == "drop-at-the-native-write":
        written = table_claims.DoorTable.write_natively

        def _race_then_write(self: table_claims.DoorTable, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — forwards the native write's own arguments and answer
            _drop_and_create_again()
            return written(self, *args, **kwargs)

        monkeypatch.setattr(table_claims.DoorTable, "write_natively", _race_then_write)
    elif door in _GATED_INSIDE_THE_DOOR:
        asked = fga_deps.require_can_drop_table

        async def _asked_then_race(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401 — forwards the gate's own arguments
            await asked(*args, **kwargs)
            await run_in_threadpool(_drop_and_create_again)

        monkeypatch.setattr(fga_deps, "require_can_drop_table", _asked_then_race)
    else:

        async def _authorize_then_race(request: Request, settings: SettingsDep, token: CurrentToken, injected: FgaClientDep, resolver: ResolverDep) -> None:
            await fga_deps.authorize(request, settings, token, injected, resolver)
            await run_in_threadpool(_drop_and_create_again)

        app.dependency_overrides[fga_deps.authorize] = _authorize_then_race
    caller = "alice" if door in _GATED_INSIDE_THE_DOOR else "bob"
    headers = {**estate.bearer(caller), **sent.get("headers", {})}
    keywords = {name: value for name, value in sent.items() if name != "headers"}

    answered = estate.client.request(method, path.format(obligation=obligation), headers=headers, **keywords)

    assert made, f"the rival never landed between bob's authorization and the door: {answered.status_code} {answered.text}"
    assert answered.status_code == 409, f"the door acted on the table created under the name after it resolved: {answered.status_code} {answered.text}"
    assert answered.json().get("code") == ConcurrentModificationError.code, answered.json()
    assert _as_alice_made_it(estate) == made[0], "the door changed the table alice created under the name"
