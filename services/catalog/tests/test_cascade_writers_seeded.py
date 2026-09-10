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
    """Settings with FGA on. The model fail-closes — `RASK_OIDC_ENABLED is required when
    RASK_FGA_ENABLED is set (authz needs a user)` — so authorization can never be switched on
    without an identity source behind it."""
    return _settings(RASK_FGA_ENABLED=True, RASK_OIDC_ENABLED=True, RASK_OIDC_ISSUER="https://dex", RASK_OIDC_AUDIENCE="rask", **overrides)


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

    async def _swallow_delete(_client, _tuples, **_kw) -> None:
        return None

    monkeypatch.setattr(fga, "write_tuples", _capture)
    # The revoke half is stubbed too, or the real `delete_tuples` reaches a client that is `object()`.
    # Asserted separately, by `_captured_deletes` — one helper per direction keeps each assertion about
    # the door it names.
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


def test_create_and_backfill_cannot_drift() -> None:
    """Both paths build their tuples from ONE function, so the estate cannot end up with two
    populations of warehouse that differ in a way nothing reports."""
    settings = _settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold", "user:service-web"])
    shared = fga_deps.cascade_tuples(settings, warehouse_id="wh1", project="acme")
    # One tuple per (subject, rung) — see `_CASCADE_RUNGS`. Asserted as the (user, relation) SET rather
    # than a user list, so adding a rung is a visible change here instead of an off-by-one on ordering.
    assert {(t.user, t.relation) for t in shared} == {
        ("project:acme", "project"),
        ("user:service-silver-to-gold", "writer"),
        ("user:service-silver-to-gold", "publisher"),
        ("user:service-silver-to-gold", "validator"),
        ("user:service-web", "writer"),
        ("user:service-web", "publisher"),
        ("user:service-web", "validator"),
    }


def test_the_backfill_records_its_own_origin() -> None:
    """An audit row claiming a tuple was written at CREATE time, when a backfill wrote it months
    later, destroys the one property the origin field exists for."""
    assert "cascade_backfill" in get_args(fga.TupleOrigin)
    assert 'origin="cascade_backfill"' in inspect.getsource(fga_deps.backfill_cascade_grants)


def test_the_rungs_are_WRITER_AND_PUBLISHER_never_owner() -> None:
    """CONTRACT (security, owner ruling 2026-09-10 — zero trust): a cascade writer gets the two rungs
    its work needs and never `owner`.

    THE OVER-GRANT THIS REPLACES, measured on the live store 2026-09-10: each of the four cascade
    identities held `owner` on 95 warehouses — every tenant in the estate — which carries `can_drop`,
    `can_deregister`, `can_restore`, `can_create_branch` and `manage_grants` on every table beneath
    them. One compromised stage runner could destroy or re-grant the whole estate's data.

    It was bought for ONE capability. The cascade calls exactly four table doors — `describe`,
    `credentials`, `register`, `publish` — and three sit at reader/writer tier. Only `publish` forced
    the owner bar, because it maps to `can_update_tag`.

    `publisher` is the narrow rung instead, and it is `can_maintain`'s precedent applied to the other
    unattended plane: a maintainer rewrites HOW a dataset is stored, a publisher advances the ref that
    says which version is BLESSED, and neither is a licence to destroy. Same granularity too — at the
    WAREHOUSE, one tuple per tenant, so the grant stays enumerable rather than estate-wide.

    `validator` is the near-miss and was tried first on the live estate: it buys `can_promote`, which
    is the OTHER door on that route (the accept-assertions override), and a stage runner holding only
    it fails identically. Pinned so the rung cannot drift to something that looks adjacent.
    """
    settings = _settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold"])
    rungs = {t.relation for t in fga_deps.cascade_tuples(settings, warehouse_id="wh1", project="acme") if t.user.startswith("user:")}
    assert rungs == {"writer", "publisher", "validator"}, f"the cascade writer holds {sorted(rungs)}"
    assert "owner" not in rungs, "a cascade writer is being granted OWNER on every tenant warehouse"


def test_the_seed_does_not_reach_the_owner_rung_for_a_SERVICE() -> None:
    """The creator is still an owner; the services are not. Asserted on the shared builder rather than
    on the seed's source text, because a source grep passes on the human's `owner` tuple sitting two
    lines away — which is exactly how the previous pin (`'"owner"' in source`) would have stayed green
    through this change."""
    settings = _settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold", "user:service-web"])
    for tup in fga_deps.cascade_tuples(settings, warehouse_id="wh1", project="acme"):
        if tup.user.startswith("user:service-"):
            assert tup.relation != "owner", f"{tup.user} was granted owner on {tup.object}"


async def _captured_deletes(fn, monkeypatch) -> list[fga.ClientTuple]:
    """Run `fn` with BOTH tuple doors stubbed, and return the tuples it tried to DELETE.

    Writes are captured and discarded here: the revoke is what this asserts, and a helper that only
    stubbed `delete_tuples` would let the real `write_tuples` reach a client that is `object()`.
    """
    deleted: list[fga.ClientTuple] = []

    async def _capture_delete(_client, tuples, **_kw) -> None:
        deleted.extend(tuples)

    async def _swallow_write(_client, _tuples, **_kw) -> None:
        return None

    monkeypatch.setattr(fga, "delete_tuples", _capture_delete)
    monkeypatch.setattr(fga, "write_tuples", _swallow_write)
    await fn()
    return deleted


@pytest.mark.asyncio
async def test_the_backfill_WITHDRAWS_the_owner_grant_it_replaces(monkeypatch) -> None:
    """Narrowing the rung the create path writes repairs nothing that already exists.

    Measured on the live store 2026-09-10: 95 warehouses, each carrying an `owner` tuple for every one
    of the four cascade identities. Writing `writer` + `publisher` beside them changes nothing an
    attacker could do — `owner` still resolves — so a backfill that only adds is a security fix that
    is true in the repository and false in the estate.

    ONLY the declared cascade subjects, and only the `owner` rung. The creator's own grant is a
    person's and is never touched; that is the same rule `cascade_tuples` states for the write side.
    """
    settings = _fga_settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold", "user:service-web"])

    deleted = await _captured_deletes(
        lambda: fga_deps.backfill_cascade_grants(
            cast("OpenFgaClient", object()), settings, warehouse_id="wh1", project="acme", actor="system:cascade-backfill"
        ),
        monkeypatch,
    )

    assert {(t.user, t.relation, t.object) for t in deleted} == {
        ("user:service-silver-to-gold", "owner", "warehouse:wh1"),
        ("user:service-web", "owner", "warehouse:wh1"),
    }


@pytest.mark.asyncio
async def test_the_narrow_grant_is_written_BEFORE_the_wide_one_is_withdrawn(monkeypatch) -> None:
    """Order is the fail-safe direction, not a detail.

    Revoke-then-grant leaves a window — and, if the grant fails, a permanent state — in which the
    cascade holds neither rung and every promotion 403s. Grant-then-revoke fails at worst back to
    today's over-granted warehouse, which is the state this repair starts from.
    """
    settings = _fga_settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold"])
    order: list[str] = []

    async def _write(_client, _tuples, **_kw) -> None:
        order.append("write")

    async def _delete(_client, _tuples, **_kw) -> None:
        order.append("delete")

    monkeypatch.setattr(fga, "write_tuples", _write)
    monkeypatch.setattr(fga, "delete_tuples", _delete)
    await fga_deps.backfill_cascade_grants(cast("OpenFgaClient", object()), settings, warehouse_id="wh1", project="acme", actor="system:cascade-backfill")
    assert order == ["write", "delete"], f"the backfill revoked before granting: {order}"
