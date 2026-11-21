"""A new warehouse is granted to its project and its maintainers at create, and a repair writes the same set.

Granted at the WAREHOUSE, not per namespace, and that is `optimize-tuples.md`'s rule rather than a
shortcut: `namespace` and `table` both define the rungs as `... or <rung> from parent`, so one tuple at
the container reaches every namespace and every table under it.

Empty by default. An estate that declares no maintenance identity gets only the project edge and the
creator's grant.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from catalog.api import fga_deps
from catalog.core.config import Settings
from lakehouse.governed import fga_ids, fga_provision
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.machine_identity import ServicePrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal


def _settings(**overrides: object) -> Settings:
    """Settings with only the fields the model REQUIRES, so this test is about the new one."""
    base = {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y"}
    return Settings.model_validate({**base, **overrides})


def _fga_settings(**overrides: object) -> Settings:
    """Settings with FGA on. The model fail-closes — `RASK_OIDC_ENABLED is required when
    RASK_FGA_ENABLED is set (authz needs a user)` — so authorization can never be switched on
    without an identity source behind it."""
    return _settings(
        RASK_FGA_ENABLED=True,
        RASK_OIDC_ENABLED=True,
        RASK_OIDC_ISSUER="https://dex",
        RASK_OIDC_AUDIENCE="rask",
        RASK_OIDC_IDP_ID="dex",
        RASK_OIDC_CONNECTORS={"local": "dex"},
        **overrides,
    )


def test_a_repair_converges_a_warehouse_to_the_grants_the_release_declares(openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """A repair writes the warehouse's project edge and the declared maintainer's rung, takes the rung back from each of the
    release's own accounts it no longer declares, leaving that account's other grants and a client's door-made rung as they
    are ([[XC-164]], [[LH-563]]).

    It grants the caller nothing: re-POSTing /management/v1/warehouses would land these tuples too, and make whoever ran
    the repair `owner` of every tenant it touched, which is why the backfill is its own door.
    """
    declared = fga_ids.user_object("kubernetes~system:serviceaccount:rask:rask-sa-maintenance")
    dropped = fga_ids.user_object("kubernetes~system:serviceaccount:rask:rask-sa-lineage")
    client = fga_ids.user_object("kubernetes~system:serviceaccount:rask:rask-sa-a-tenant-service")
    openfga_store.write((dropped, "maintenance", "warehouse:wh1"), (dropped, "reader", "warehouse:wh1"), (client, "maintenance", "warehouse:wh1"))
    settings = _fga_settings(
        LANCE_FGA_MAINTAINERS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance"],
        LANCE_OWN_ACCOUNTS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance", "kubernetes~system:serviceaccount:rask:rask-sa-lineage"],
    )

    async def _repair() -> None:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as store:
            await fga_deps.converge_warehouse_grants(store, settings, warehouse_id="wh1", project="acme", actor="system:warehouse-grant-backfill")

    asyncio.run(_repair())

    assert openfga_store.tuples() == {
        ("project:acme", "project", "warehouse:wh1"),
        (declared, "maintenance", "warehouse:wh1"),
        (dropped, "reader", "warehouse:wh1"),
        (client, "maintenance", "warehouse:wh1"),
    }


_PERSON = PersonPrincipal(
    principal=Principal(idp_id="dex", subject="alice"),
    claims=IDToken.model_validate({"sub": "alice", "iss": "https://dex", "aud": "rask", "iat": 0, "exp": 1}),
)
_MACHINE = ServicePrincipal.for_account("system:serviceaccount:etl:loader")


@pytest.mark.parametrize(
    ("creator", "owners"),
    [
        pytest.param(_PERSON, {("user:dex~alice", "owner", "warehouse:wh1")}, id="a-person-owns-the-warehouse-they-create"),
        pytest.param(_MACHINE, set(), id="a-machine-owns-nothing-its-project-administers-what-it-creates"),
    ],
)
def test_creating_a_warehouse_grants_it(
    openfga_store: Any,  # noqa: ANN401 — the root conftest's OpenFgaStore
    creator: PersonPrincipal | ServicePrincipal,
    owners: set[tuple[str, str, str]],
) -> None:
    """A warehouse is maintainable the moment it exists, or its tenant's data is never compacted.

    [[LH-165]]. `warehouse.maintenance` has no `from parent`, so the sweep reaches a tenant's tables
    through exactly one tuple, `warehouse:<id>#maintenance@<maintenance identity>`, and the create door is
    where it has to be written: there is then no window in which a tenant exists and its data cannot be
    maintained. It rides in `cascade_tuples`, which the create door and `converge_warehouse_grants` share,
    so the two populations of warehouse cannot differ. Measured 2026-09-15 on the live estate: the 4 of
    97 warehouses without the tuple were the 4 newest, every one created after the hand-written grants.

    Only a person is made the owner ([[LH-433]], [[LH-585]]): a machine that creates a warehouse holds nothing on it by
    creating it, as with a table or a project, and its project's admins own it through the project edge. Nothing else is
    written, a project admin grant least of all: that is minted only at POST /management/v1/projects, behind the estate
    gate, so a warehouse's owner never escalates to its tenant's admin by creating one.
    """
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance"])

    async def _create() -> None:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as store:
            await fga_deps.seed_warehouse(store, settings, creator, warehouse_id="wh1", project="acme")

    asyncio.run(_create())

    assert openfga_store.tuples() == owners | {
        ("project:acme", "project", "warehouse:wh1"),
        ("user:kubernetes~system%3Aserviceaccount%3Arask%3Arask-sa-maintenance", "maintenance", "warehouse:wh1"),
    }
