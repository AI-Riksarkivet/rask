"""A warehouse is maintainable the moment it exists, or its tenant's data is never compacted.

[[LH-165]]. `table.maintainer` is `[...] or owner or maintainer from parent` and `warehouse.maintainer`
is `[...] or owner` with no `from parent`, so the sweep reaches a tenant's tables through exactly one
tuple: `warehouse:<id>#maintainer@user:service-maintenance`. **No code wrote it.** `seed_warehouse`
emitted the creator's `owner` plus `cascade_tuples()` — the `project` edge and `_CASCADE_RUNGS` for the
declared cascade writers, a list that is the medallion producer and its stage runners and never
maintenance. `backfill_cascade_grants` reuses the same function, so the repair path wrote the same
empty set. The only committed writer is the Helm hook, and it writes ONE tuple on ONE fixed object,
`FGA_ROOT_OBJECT` = `warehouse:lance_catalog`, which reaches no other warehouse.

MEASURED 2026-09-15 on the live estate, and the count reads backwards. 93 of 97 warehouses carried the
tuple and 4 did not, which invites "repair the four". Sorted by the registry's own `created_at` the
four are the four NEWEST (2026-09-10 x3, 2026-09-14) and every granted warehouse is older, with a
clean cutoff and no exceptions: newest granted `2026-09-06T04:42Z`, oldest ungranted
`2026-09-10T08:36Z`. The 92 were written by hand into the store on 2026-09-08 (see
`tests/integration/test_the_maintainer_rung_opens_the_write_tier_vend.py`). So 93 is the artefact and
4 is the software: every warehouse created since has no grant, and the next tenant onboarded adds one.

IN `cascade_tuples`, NOT BESIDE IT, and that is the whole point. The create door and
`backfill_cascade_grants` share that one function precisely so the two populations cannot differ —
its own docstring says a backfill writing a different set than creates write "is worse than no
backfill, because the estate then has two populations of warehouse that differ in a way nothing
reports". A maintainer grant added only to the create path would recreate that split immediately.

AT THE WAREHOUSE, one tuple per tenant — `optimize-tuples.md`'s rule, the same reason the cascade
rungs live there: `namespace` and `table` both define `maintainer ... or maintainer from parent`, so
one tuple reaches every tier and every table below it.

EMPTY BY DEFAULT. An estate declaring no maintenance identity gets exactly today's tuples, so this
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
    base = {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y"}
    return Settings.model_validate({**base, **overrides})


def _fga_settings(**overrides: object) -> Settings:
    return _settings(RASK_FGA_ENABLED=True, RASK_OIDC_ENABLED=True, RASK_OIDC_ISSUER="https://dex", RASK_OIDC_AUDIENCE="rask", **overrides)


async def _captured_tuples(fn, monkeypatch) -> list[fga.ClientTuple]:
    seen: list[fga.ClientTuple] = []

    async def _capture(_client, tuples, **_kw) -> None:
        seen.extend(tuples)

    async def _swallow_delete(_client, _tuples, **_kw) -> None:
        return None

    monkeypatch.setattr(fga, "write_tuples", _capture)
    monkeypatch.setattr(fga, "delete_tuples", _swallow_delete)
    await fn()
    return seen


def test_settings_declare_the_maintenance_identities() -> None:
    assert "fga_maintainers" in Settings.model_fields


def test_it_is_empty_by_default() -> None:
    """No declared identity means no extra tuple — merging this changes no existing estate."""
    assert _settings().fga_maintainers == []


def test_declared_identities_are_taken_verbatim() -> None:
    """The catalog must not invent the naming convention of a plane it does not own."""
    s = _settings(LANCE_FGA_MAINTAINERS=["user:service-maintenance"])
    assert s.fga_maintainers == ["user:service-maintenance"]


def test_the_tuple_set_carries_the_maintainer_grant() -> None:
    """THE DEFECT: `cascade_tuples` returned no `maintainer` tuple, for any configuration."""
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["user:service-maintenance"])

    written = fga_deps.cascade_tuples(settings, warehouse_id="wh1", project="acme")

    assert ("user:service-maintenance", "maintainer", "warehouse:wh1") in [(t.user, t.relation, t.object) for t in written]


def test_the_grant_is_on_the_WAREHOUSE_not_a_tier() -> None:
    """One tuple per tenant. A per-tier grant is the tuple explosion `optimize-tuples.md` refuses, and
    the hierarchy already implies it: `namespace` and `table` both define `maintainer from parent`."""
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["user:service-maintenance"])

    objects = {t.object for t in fga_deps.cascade_tuples(settings, warehouse_id="wh1", project="acme") if t.relation == "maintainer"}

    assert objects == {"warehouse:wh1"}


def test_maintenance_is_never_granted_a_DESTRUCTIVE_rung() -> None:
    """`maintainer`, never `owner` or `writer`. Maintenance rewrites HOW a dataset is stored and must
    not be able to drop it or change what it says — the separation `can_maintain` exists to express."""
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["user:service-maintenance"])

    rungs = {t.relation for t in fga_deps.cascade_tuples(settings, warehouse_id="wh1", project="acme") if t.user == "user:service-maintenance"}

    assert rungs == {"maintainer"}


@pytest.mark.asyncio
async def test_creating_a_warehouse_grants_it(monkeypatch) -> None:
    """The grant happens where the container is created, so there is no window in which a tenant
    exists and its data cannot be maintained."""
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["user:service-maintenance"])
    token = IDToken.model_validate({"sub": "alice", "iss": "https://dex", "aud": "rask", "iat": 0, "exp": 1})

    seen = await _captured_tuples(
        lambda: fga_deps.seed_warehouse(cast("OpenFgaClient", object()), settings, token, warehouse_id="wh1", project="acme"),
        monkeypatch,
    )

    assert ("user:service-maintenance", "maintainer", "warehouse:wh1") in [(t.user, t.relation, t.object) for t in seen]


@pytest.mark.asyncio
async def test_the_BACKFILL_writes_the_same_grant_as_the_create(monkeypatch) -> None:
    """The two populations must not differ. This is the property `cascade_tuples` exists to hold, and
    the one a maintainer grant bolted onto the create door alone would break on the day it landed:
    the 4 warehouses measured without the tuple can only be repaired through the backfill."""
    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["user:service-maintenance"])

    seen = await _captured_tuples(
        lambda: fga_deps.backfill_cascade_grants(cast("OpenFgaClient", object()), settings, warehouse_id="wh1", project="acme", actor="ops"),
        monkeypatch,
    )

    assert ("user:service-maintenance", "maintainer", "warehouse:wh1") in [(t.user, t.relation, t.object) for t in seen]


@pytest.mark.asyncio
async def test_a_maintenance_only_estate_still_runs_the_backfill(monkeypatch) -> None:
    """The guard must not skip on the cascade list alone, now that the maintainer grant rides with it.

    `backfill` early-returns when there is nothing to grant, and that guard read `fga_cascade_writers`
    only. With the maintainer grant in `cascade_tuples`, an estate that declares a maintenance identity
    and no stage runners would skip the whole backfill and log that it had nothing to do — the same
    silent-skip shape that let this grant go unwritten for a week.
    """
    from catalog.services import cascade_backfill

    settings = _fga_settings(LANCE_FGA_MAINTAINERS=["user:service-maintenance"])  # and NO cascade writers
    skipped: list[str] = []

    def _log_info(event: str, **kw: object) -> None:
        if event == "cascade_backfill_skipped":
            skipped.append(str(kw.get("extra") or {}))

    monkeypatch.setattr(cascade_backfill.log, "info", lambda event, **kw: _log_info(event, **kw))

    async def _no_client(*_a: object, **_kw: object) -> None:
        raise RuntimeError("reached the client build, so the guard did not skip")

    monkeypatch.setattr(cascade_backfill, "build_fga_client", _no_client)

    with pytest.raises(RuntimeError, match="did not skip"):
        await cascade_backfill.backfill(settings)

    assert skipped == [], f"a maintenance-only estate was skipped: {skipped}"
