"""A new warehouse grants the cascade's own identities, or its tenant cannot reach gold.

Measured five separate times on the live estate before this existed: a tenant is created, its tiers
are created, a cascade runs — and dies at `403 can_update_tag` in a stage runner log nobody is watching.
The estate looks healthy the whole time. Rows land in bronze, lineage records the run, the UI shows
the table; only the publish is refused, and only the log says so.

The cause is that the seed grants PEOPLE. Every service that actually moves data between tiers had to
be discovered by watching it fail: the bronze->silver stage runner, the silver->gold stage runner, the PRODUCER
(which is what resumes a human-approved promotion, so an approval 403'd AFTER someone said yes), and
`service-web` (which reads lineage back).

Granted at the WAREHOUSE, not per tier, and that is `optimize-tuples.md`'s rule rather than a
shortcut: `namespace` and `table` both define these rungs as `... or <rung> from parent`, so one
tuple at the container reaches every tier and every table under it. Per-tier grants would be
three-plus tuples per tenant that hierarchy already implies.

THE RUNGS ARE `writer` + `publisher` + `validator`, NEVER `owner` (owner ruling 2026-09-10, zero
trust). The cascade calls four table doors — `describe`, `credentials`, `register`, `publish` — and
only the last forced the owner bar, because `can_update_tag` had no rung beneath it. It does now.
`validator` stays because `/publish` has a second door: an `accept_assertions` body needs
`can_promote`, and that is how the producer resumes a promotion a HUMAN approved. See `_CASCADE_RUNGS`.

Empty by default. An estate that declares no cascade identities gets exactly today's tuples, so this
cannot change an existing deployment's authorization by being merged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from catalog.api import fga_deps
from catalog.core.config import Settings
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


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
    return _settings(RASK_FGA_ENABLED=True, RASK_OIDC_ENABLED=True, RASK_OIDC_ISSUER="https://dex", RASK_OIDC_AUDIENCE="rask", **overrides)


async def _captured_tuples(fn, monkeypatch) -> list[fga.ClientTuple]:
    """Run `fn` with `write_tuples` stubbed, and return the tuples it tried to write.

    This replaces an `inspect.getsource(...)` string match. That assertion broke the moment the tuple
    set was extracted into `cascade_tuples` for the backfill to share — while the BEHAVIOUR it was
    guarding was completely intact. A test that fails on a refactor it should not notice, and would
    pass on a `fga_cascade_writers` mentioned in a comment, was testing the wrong thing; asserting the
    submitted tuples is strictly stronger.
    """
    seen: list[fga.ClientTuple] = []

    async def _capture(_client, tuples, **_kw) -> None:
        seen.extend(tuples)

    async def _swallow_delete(_client, _tuples, **_kw) -> None:
        return None

    monkeypatch.setattr(fga, "write_tuples", _capture)
    # The revoke half is stubbed too, or the real `delete_tuples` reaches a client that is `object()`.
    monkeypatch.setattr(fga, "delete_tuples", _swallow_delete)
    await fn()
    return seen


@pytest.mark.asyncio
async def test_seed_warehouse_grants_them(monkeypatch) -> None:
    """The grant has to happen where the container is created; a later hook would leave a window in
    which the tenant exists and its cascade cannot publish into it."""
    settings = _fga_settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold"])
    token = IDToken.model_validate({"sub": "alice", "iss": "https://dex", "aud": "rask", "iat": 0, "exp": 1})

    seen = await _captured_tuples(
        lambda: fga_deps.seed_warehouse(cast("OpenFgaClient", object()), settings, token, warehouse_id="wh1", project="acme"),
        monkeypatch,
    )

    assert fga.ClientTuple(user="user:service-silver-to-gold", relation="writer", object="warehouse:wh1") in seen
    assert fga.ClientTuple(user="user:service-silver-to-gold", relation="publisher", object="warehouse:wh1") in seen
    assert fga.ClientTuple(user="project:acme", relation="project", object="warehouse:wh1") in seen
    # The CREATOR is still an owner — the narrowing is about the unattended services, not the person.
    assert fga.ClientTuple(user="user:alice", relation="owner", object="warehouse:wh1") in seen


@pytest.mark.asyncio
async def test_the_backfill_writes_the_SAME_grants_minus_the_caller(monkeypatch) -> None:
    """Repair must not require becoming an owner of what you repair.

    Re-POSTing /v1/warehouses would land these tuples too — and grant whoever ran the repair `owner`
    on every tenant they touched. That is why the backfill exists as its own door rather than as
    "just run the create again".
    """
    settings = _fga_settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold"])

    seen = await _captured_tuples(
        lambda: fga_deps.backfill_cascade_grants(
            cast("OpenFgaClient", object()), settings, warehouse_id="wh1", project="acme", actor="system:cascade-backfill"
        ),
        monkeypatch,
    )

    assert fga.ClientTuple(user="user:service-silver-to-gold", relation="writer", object="warehouse:wh1") in seen
    assert fga.ClientTuple(user="user:service-silver-to-gold", relation="publisher", object="warehouse:wh1") in seen
    assert fga.ClientTuple(user="project:acme", relation="project", object="warehouse:wh1") in seen
    assert not [t for t in seen if t.user.startswith("user:") and "service-" not in t.user], f"the backfill granted a non-service subject: {seen}"


@pytest.mark.asyncio
async def test_creating_a_warehouse_grants_it(monkeypatch) -> None:
    """A warehouse is maintainable the moment it exists, or its tenant's data is never compacted.

    [[LH-165]]. `warehouse.maintainer` has no `from parent`, so the sweep reaches a tenant's tables
    through exactly one tuple, `warehouse:<id>#maintainer@<maintenance identity>`, and the create door is
    where it has to be written: there is then no window in which a tenant exists and its data cannot be
    maintained. It rides in `cascade_tuples`, which the create door and `backfill_cascade_grants` share,
    so the two populations of warehouse cannot differ. Measured 2026-09-15 on the live estate: the 4 of
    97 warehouses without the tuple were the 4 newest, every one created after the hand-written grants.
    """
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["user:service-maintenance"])
    token = IDToken.model_validate({"sub": "alice", "iss": "https://dex", "aud": "rask", "iat": 0, "exp": 1})

    seen = await _captured_tuples(
        lambda: fga_deps.seed_warehouse(cast("OpenFgaClient", object()), settings, token, warehouse_id="wh1", project="acme"),
        monkeypatch,
    )

    assert ("user:service-maintenance", "maintainer", "warehouse:wh1") in [(t.user, t.relation, t.object) for t in seen]
