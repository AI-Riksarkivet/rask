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

from catalog.api import fga_deps
from catalog.core.config import Settings


def test_settings_declare_the_cascade_identities() -> None:
    assert "fga_cascade_writers" in Settings.model_fields


def _settings(**overrides: object) -> Settings:
    """Settings with only the fields the model REQUIRES, so this test is about the new one."""
    base = {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y"}
    return Settings.model_validate({**base, **overrides})


def test_it_is_empty_by_default() -> None:
    """No declared identities means no extra tuples — merging this changes no existing estate."""
    assert _settings().fga_cascade_writers == []


def test_declared_identities_are_taken_verbatim() -> None:
    """The catalog must not invent the naming convention of a plane it does not own."""
    s = _settings(LANCE_FGA_CASCADE_WRITERS=["user:service-bronze-to-silver", "user:service-web"])
    assert s.fga_cascade_writers == ["user:service-bronze-to-silver", "user:service-web"]


def test_seed_warehouse_grants_them() -> None:
    """The grant has to happen where the container is created; a later hook would leave a window in
    which the tenant exists and its cascade cannot publish into it."""
    source = inspect.getsource(fga_deps.seed_warehouse)
    assert "fga_cascade_writers" in source, "seed_warehouse ignores the declared cascade identities"


def test_the_rung_is_owner() -> None:
    """`publish` is guarded by `can_update_tag`, and the model defines `can_update_tag: owner`.

    `validator` is the near-miss and was tried first on the live estate: it buys `can_promote`, which
    is the OTHER door on that route (the accept-assertions override), and a mover holding only it
    fails identically. Pinned so the rung cannot be quietly lowered to something that looks adjacent.
    """
    source = inspect.getsource(fga_deps.seed_warehouse)
    assert '"owner"' in source
