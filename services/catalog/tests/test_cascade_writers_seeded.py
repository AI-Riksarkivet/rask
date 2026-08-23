"""A new warehouse grants the cascade's own identities, or its tenant cannot reach gold.

Measured five separate times on the live estate before this existed: a tenant is created, its tiers
are created, a cascade runs — and dies at `403 can_update_tag` in a mover log nobody is watching.
The estate looks healthy the whole time. Rows land in bronze, lineage records the run, the UI shows
the table; only the publish is refused, and only the log says so.

The cause is that the seed grants PEOPLE. Every service that actually moves data between tiers had to
be discovered by watching it fail: the bronze->silver mover, the silver->gold mover, the PRODUCER
(which is what resumes a human-approved promotion, so an approval 403'd AFTER someone said yes), and
`service-web` (which reads lineage back).

Granted at the WAREHOUSE, not per tier, and that is `optimize-tuples.md`'s rule rather than a
shortcut: `namespace` defines `owner: ... or owner from parent` and `table` the same, so one tuple at
the container reaches every tier and every table under it. Per-tier grants would be three-plus tuples
per tenant that hierarchy already implies.

Empty by default. An estate that declares no cascade identities gets exactly today's tuples, so this
cannot change an existing deployment's authorization by being merged.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, cast, get_args

import pytest
from catalog.api import fga_deps
from catalog.core.config import Settings

from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


def test_settings_declare_the_cascade_identities() -> None:
    assert "fga_cascade_writers" in Settings.model_fields


def _settings(**overrides: object) -> Settings:
    """Settings with only the fields the model REQUIRES, so this test is about the new one."""
    base = {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y"}
    return Settings.model_validate({**base, **overrides})


def _fga_settings(**overrides: object) -> Settings:
    """Settings with FGA on. The model fail-closes — `LANCE_OIDC_ENABLED is required when
    LANCE_FGA_ENABLED is set (authz needs a user)` — so authorization can never be switched on
    without an identity source behind it."""
    return _settings(LANCE_FGA_ENABLED=True, LANCE_OIDC_ENABLED=True, LANCE_OIDC_ISSUER="https://dex", LANCE_OIDC_AUDIENCE="rask", **overrides)


def test_it_is_empty_by_default() -> None:
    """No declared identities means no extra tuples — merging this changes no existing estate."""
    assert _settings().fga_cascade_writers == []


def test_declared_identities_are_taken_verbatim() -> None:
    """The catalog must not invent the naming convention of a plane it does not own."""
    s = _settings(LANCE_FGA_CASCADE_WRITERS=["user:service-bronze-to-silver", "user:service-web"])
    assert s.fga_cascade_writers == ["user:service-bronze-to-silver", "user:service-web"]


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

    monkeypatch.setattr(fga, "write_tuples", _capture)
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

    assert fga.ClientTuple(user="user:service-silver-to-gold", relation="owner", object="warehouse:wh1") in seen
    assert fga.ClientTuple(user="project:acme", relation="project", object="warehouse:wh1") in seen
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

    assert fga.ClientTuple(user="user:service-silver-to-gold", relation="owner", object="warehouse:wh1") in seen
    assert fga.ClientTuple(user="project:acme", relation="project", object="warehouse:wh1") in seen
    assert not [t for t in seen if t.user.startswith("user:") and "service-" not in t.user], f"the backfill granted a non-service subject: {seen}"


def test_create_and_backfill_cannot_drift() -> None:
    """Both paths build their tuples from ONE function, so the estate cannot end up with two
    populations of warehouse that differ in a way nothing reports."""
    settings = _settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold", "user:service-web"])
    shared = fga_deps.cascade_tuples(settings, warehouse_id="wh1", project="acme")
    assert [t.user for t in shared] == ["project:acme", "user:service-silver-to-gold", "user:service-web"]


def test_the_backfill_records_its_own_origin() -> None:
    """An audit row claiming a tuple was written at CREATE time, when a backfill wrote it months
    later, destroys the one property the origin field exists for."""
    assert "cascade_backfill" in get_args(fga.TupleOrigin)
    assert 'origin="cascade_backfill"' in inspect.getsource(fga_deps.backfill_cascade_grants)


def test_the_rung_is_owner() -> None:
    """`publish` is guarded by `can_update_tag`, and the model defines `can_update_tag: owner`.

    `validator` is the near-miss and was tried first on the live estate: it buys `can_promote`, which
    is the OTHER door on that route (the accept-assertions override), and a mover holding only it
    fails identically. Pinned so the rung cannot be quietly lowered to something that looks adjacent.
    """
    source = inspect.getsource(fga_deps.seed_warehouse)
    assert '"owner"' in source
