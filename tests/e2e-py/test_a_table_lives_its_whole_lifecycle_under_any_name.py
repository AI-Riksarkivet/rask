"""[[LH-527]] A table lives its whole lifecycle on the estate whatever its name holds (ADR 0173; LH-512 #684 clauses 2
and 5).

Each name holds what a key spelled from a name mishandles, by its own escaping rules: `räksmörgås` a non-ASCII
character, which a URL carries percent-encoded while the store keeps it decoded; `growth%` the escape character itself;
`sp ace` a space, which OpenFGA refuses in an object id and pylance's Rust client sends as `+`. No name reaches a storage
path, a credential's policy or an FGA object: the table's uuid does (ADR 0173). So on the deployed estate each name lives
the whole lifecycle through the doors:

- a stock client makes the table, writes it and reads it back on the credential the catalog vends;
- a rename keeps its uuid, and lineage's feed, asked by the new name, holds the writes made under both names;
- the table's own policy, set under the old name to keep one version, expires the versions written under it: the
  estate's default keeps every version for 7 days (`MAINTENANCE_OLDER_THAN_DAYS`), so nothing else expires one this
  young;
- a drop is recoverable, the undrop restores its rows, and a purge leaves no table and nothing to undrop.

Driven as the estate's admin in a scratch tenant of its own, removed at the end. Run with
`bash scripts/e2e_live.sh tests/e2e-py/test_a_table_lives_its_whole_lifecycle_under_any_name.py`.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from urllib.parse import quote

import byo_scenario as scenario
import lance
import lance_namespace
import pyarrow as pa
import pytest
import requests
from scratch_tenant import CATALOG, DEX, Identity, Tenant, ok, provisioned

from service_kit.lakehouse.naming import CATALOG_DELIMITER, uuid_of


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (CATALOG and DEX and scenario.LINEAGE),
        reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_LINEAGE_URL and LANCE_E2E_DEX (a deployed estate)",
    ),
]

#: The version the test writes up to. Maintenance's write-event lane evaluates a table at its first write and again once
#: ten more versions have landed (`MAINTENANCE_EVENT_MIN_VERSIONS`), and expires only once twenty are reclaimable
#: (`maintenance.minVersionsToExpire`). Under a policy keeping one version, an evaluation at version 21 or later finds
#: twenty, and by version 32 one has run whichever version the lane first evaluated. The lane expires only against a
#: clone list younger than `MAINTENANCE_CLONE_LIST_MAX_AGE_SECONDS` (3600 s); the sweep writes one every
#: `maintenance.schedule` (1800 s), and the e2e-stack lane runs the first sweep itself.
LAST_VERSION = 32


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("lh527", grants=())


def _path(table_id: list[str]) -> str:
    """``table_id`` as the ``{id}`` of a door's path: joined by the delimiter and percent-encoded, a space as ``%20``."""
    return quote(CATALOG_DELIMITER.join(table_id), safe=CATALOG_DELIMITER)


def _described(admin: Identity, table_id: list[str]) -> requests.Response:
    return admin.http.post(f"{CATALOG}/v1/table/{_path(table_id)}/describe", json={})


def _append(client: lance_namespace.LanceNamespace, table_id: list[str], row: int) -> None:
    """One commit, holding the one row ``row``, by a stock client on the credential the catalog vends."""
    lance.write_dataset(pa.table({"id": pa.array([row], pa.int64())}), namespace_client=client, table_id=table_id, mode="append")


def _ids(client: lance_namespace.LanceNamespace, table_id: list[str]) -> list[int]:
    """The rows a stock client reads from the table's newest version, on the credential the catalog vends."""
    return sorted(lance.dataset(namespace_client=client, table_id=table_id).to_table(columns=["id"]).column("id").to_pylist())


def _versions_once_gone(client: lance_namespace.LanceNamespace, table_id: list[str], gone: set[int]) -> set[int]:
    """The table's versions, read until none of ``gone`` is among them or the settle window has passed."""
    deadline = time.monotonic() + scenario.SETTLE_SECONDS
    while True:
        versions = {int(listed["version"]) for listed in lance.dataset(namespace_client=client, table_id=table_id).versions()}
        if gone.isdisjoint(versions) or time.monotonic() > deadline:
            return versions
        time.sleep(2)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("räksmörgås", id="non-ascii"),
        pytest.param("growth%", id="percent-sign"),
        pytest.param("sp ace", id="space"),
    ],
)
def test_a_table_lives_its_whole_lifecycle_under_any_name(tenant: Tenant, name: str) -> None:
    admin, client = tenant.admin, tenant.admin.namespace()
    feed_reader = scenario.Identity(admin.bearer)
    made, renamed = [tenant.bronze, name], [tenant.bronze, f"{name} renamed"]
    since = scenario.feed_start(feed_reader)

    # A stock client makes the table, writes it and reads it, at versions 1 and 2.
    lance.write_dataset(
        pa.table({"id": pa.array([1], pa.int64())}),
        namespace_client=client,
        table_id=made,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    tenant.tables.append(CATALOG_DELIMITER.join(made))
    _append(client, made, 2)
    assert _ids(client, made) == [1, 2]
    table_uuid = uuid_of(ok(_described(admin, made))["properties"])
    assert table_uuid is not None, f"the catalog answers no uuid for {made}"

    # Its own policy, set under the name it was made with, keeps one version.
    ok(admin.http.post(f"{CATALOG}/management/v1/table/{_path(made)}/policy/set", json={"retain_versions": 1}))

    # A rename keeps the table: its uuid and its rows, under the new name only.
    ok(admin.http.post(f"{CATALOG}/v1/table/{_path(made)}/rename", json={"new_table_name": renamed[-1]}))
    tenant.tables.remove(CATALOG_DELIMITER.join(made))
    tenant.tables.append(CATALOG_DELIMITER.join(renamed))
    assert _described(admin, made).status_code == 404
    assert uuid_of(ok(_described(admin, renamed))["properties"]) == table_uuid
    _append(client, renamed, 3)

    # One lineage: the feed, asked by the new name, holds the writes made under both names, each naming the one uuid.
    rows, _ = scenario.follow(
        feed_reader, since=since, table=CATALOG_DELIMITER.join(renamed), until=lambda rows: {1, 2, 3} <= {v for _, v in scenario.writes(rows)}
    )
    written = {int(scenario.lance_facet(row)["version"]): row for row in rows if "version" in scenario.lance_facet(row)}
    assert {version: (written[version]["outputs"], written[version]["table_uuids"]) for version in (1, 2, 3)} == {
        1: ([CATALOG_DELIMITER.join(made)], [table_uuid]),
        2: ([CATALOG_DELIMITER.join(made)], [table_uuid]),
        3: ([CATALOG_DELIMITER.join(renamed)], [table_uuid]),
    }

    # Its own policy expires it: the versions written under the old name go, and every row stays.
    for row in range(4, LAST_VERSION + 1):
        _append(client, renamed, row)
    versions = _versions_once_gone(client, renamed, gone={1, 2})
    assert {1, 2}.isdisjoint(versions), f"versions 1 and 2 of {renamed} outlived its own policy: {sorted(versions)}"
    assert _ids(client, renamed) == list(range(1, LAST_VERSION + 1))

    # A drop is recoverable: the undrop restores the table it dropped, rows and uuid.
    ok(admin.http.post(f"{CATALOG}/v1/table/{_path(renamed)}/drop", json={}))
    assert _described(admin, renamed).status_code == 404
    ok(admin.http.post(f"{CATALOG}/management/v1/table/{_path(renamed)}/undrop", json={}))
    assert uuid_of(ok(_described(admin, renamed))["properties"]) == table_uuid
    assert _ids(client, renamed) == list(range(1, LAST_VERSION + 1))

    # A purge destroys it: no table answers to the name, and nothing is left to undrop, so the undrop that restored it
    # above is refused as on any name that holds nothing (only a read door answers an absent name 404, ADR 0173 §8).
    ok(admin.http.post(f"{CATALOG}/v1/table/{_path(renamed)}/drop", params={"purge": "true"}, json={}))
    tenant.tables.remove(CATALOG_DELIMITER.join(renamed))
    assert _described(admin, renamed).status_code == 404
    assert admin.http.post(f"{CATALOG}/management/v1/table/{_path(renamed)}/undrop", json={}).status_code == 403
