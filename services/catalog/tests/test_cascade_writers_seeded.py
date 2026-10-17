"""A new warehouse is granted to its project and its maintainers at create, and a repair writes the same set.

Granted at the WAREHOUSE, not per tier, and that is `optimize-tuples.md`'s rule rather than a
shortcut: `namespace` and `table` both define the rungs as `... or <rung> from parent`, so one tuple at
the container reaches every tier and every table under it.

Empty by default. An estate that declares no maintenance identity gets only the project edge and the
creator's grant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from catalog.api import fga_deps
from catalog.core.config import Settings
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken
from service_kit.governed.person_identity import PersonPrincipal
from service_kit.governed.principal import Principal


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


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
        RASK_OIDC_CONNECTORS={"local": "dex"},
        **overrides,
    )


async def _captured_tuples(fn, monkeypatch) -> list[fga.ClientTuple]:
    """Run `fn` with `write_tuples` stubbed, and return the tuples it tried to write.

    This replaces an `inspect.getsource(...)` string match. That assertion broke the moment the tuple
    set was extracted into `cascade_tuples` for the backfill to share — while the BEHAVIOUR it was
    guarding was completely intact. Asserting the submitted tuples is strictly stronger.
    """
    seen: list[fga.ClientTuple] = []

    async def _capture(_client, tuples, **_kw) -> None:
        seen.extend(tuples)

    monkeypatch.setattr(fga, "write_tuples", _capture)
    await fn()
    return seen


@pytest.mark.asyncio
async def test_the_backfill_writes_the_SAME_grants_minus_the_caller(monkeypatch) -> None:
    """Repair must not require becoming an owner of what you repair.

    Re-POSTing /management/v1/warehouses would land these tuples too — and grant whoever ran the repair `owner`
    on every tenant they touched. That is why the backfill exists as its own door rather than as
    "just run the create again".
    """
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance"])

    seen = await _captured_tuples(
        lambda: fga_deps.backfill_cascade_grants(
            cast("OpenFgaClient", object()), settings, warehouse_id="wh1", project="acme", actor="system:cascade-backfill"
        ),
        monkeypatch,
    )

    assert fga.ClientTuple(user="user:kubernetes~system%3Aserviceaccount%3Arask%3Arask-sa-maintenance", relation="maintenance", object="warehouse:wh1") in seen
    assert fga.ClientTuple(user="project:acme", relation="project", object="warehouse:wh1") in seen
    assert not [t for t in seen if t.user.startswith("user:") and not t.user.startswith("user:kubernetes~")], (
        f"the backfill granted a non-service subject: {seen}"
    )


@pytest.mark.asyncio
async def test_creating_a_warehouse_grants_it(monkeypatch) -> None:
    """A warehouse is maintainable the moment it exists, or its tenant's data is never compacted.

    [[LH-165]]. `warehouse.maintenance` has no `from parent`, so the sweep reaches a tenant's tables
    through exactly one tuple, `warehouse:<id>#maintenance@<maintenance identity>`, and the create door is
    where it has to be written: there is then no window in which a tenant exists and its data cannot be
    maintained. It rides in `cascade_tuples`, which the create door and `backfill_cascade_grants` share,
    so the two populations of warehouse cannot differ. Measured 2026-09-15 on the live estate: the 4 of
    97 warehouses without the tuple were the 4 newest, every one created after the hand-written grants.
    """
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance"])
    token = PersonPrincipal(
        principal=Principal(idp_id="dex", subject="alice"),
        claims=IDToken.model_validate({"sub": "alice", "iss": "https://dex", "aud": "rask", "iat": 0, "exp": 1}),
    )

    seen = await _captured_tuples(
        lambda: fga_deps.seed_warehouse(cast("OpenFgaClient", object()), settings, token, warehouse_id="wh1", project="acme"),
        monkeypatch,
    )

    assert ("user:kubernetes~system%3Aserviceaccount%3Arask%3Arask-sa-maintenance", "maintenance", "warehouse:wh1") in [
        (t.user, t.relation, t.object) for t in seen
    ]
