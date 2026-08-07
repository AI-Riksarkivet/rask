"""`adopt_existing` accepts a bytes-first migration WITHOUT relaxing either hazard guard.

The #54 flow is: copy the datasets into the warehouse bucket, THEN bind. Before this flag the door
had no way to say "the physical namespace preceded its governance record" — a migrated-in namespace
409'd forever (measured live: silver). The flag converges exactly that case; a typo'd create without
it still refuses, because silently adopting would inherit a stranger's data.
"""

from __future__ import annotations

import inspect

from catalog.api.v1.endpoints import warehouses as warehouse_routes
from catalog.schemas import CreateWarehouseNamespaceRequest


def test_default_is_refusal_not_adoption() -> None:
    """Silent adoption is the hazard; it must be OPTED INTO per request."""
    assert CreateWarehouseNamespaceRequest(namespace="x").adopt_existing is False


def test_adoption_never_bypasses_the_hazard_guards() -> None:
    """Both guards run BEFORE the native create the flag converges on.

    Source-order proof (the same technique test_seed_estate.py:269 uses on this endpoint): the
    write-once binding guard and the default-root collision guard must appear before the
    `adopt_existing` branch, so the flag can only ever accept a namespace that passed both.
    """
    src = inspect.getsource(warehouse_routes.create_warehouse_namespace)
    write_once = src.index("already bound to another warehouse")
    default_root = src.index("already exists in the default root")
    adoption = src.index("adopt_existing")
    assert write_once < adoption
    assert default_root < adoption


def test_adoption_branch_converges_only_the_warehouse_root_conflict() -> None:
    """The except targets NamespaceAlreadyExistsError from the WAREHOUSE-rooted create — nothing
    else. A different failure (storage down, permission) must still surface."""
    src = inspect.getsource(warehouse_routes.create_warehouse_namespace)
    assert "except NamespaceAlreadyExistsError" in src
    assert "if not body.adopt_existing:\n            raise" in src
