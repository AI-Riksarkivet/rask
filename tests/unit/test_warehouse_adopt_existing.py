"""`adopt_existing` accepts a bytes-first migration WITHOUT relaxing either hazard guard.

The #54 flow is: copy the datasets into the warehouse bucket, THEN bind. Before this flag the door
had no way to say "the physical namespace preceded its governance record" — a migrated-in namespace
409'd forever (measured live: silver). The flag converges exactly that case; a typo'd create without
it still refuses, because silently adopting would inherit a stranger's data.
"""

from __future__ import annotations

from catalog.schemas import CreateWarehouseNamespaceRequest


def test_default_is_refusal_not_adoption() -> None:
    """Silent adoption is the hazard; it must be OPTED INTO per request."""
    assert CreateWarehouseNamespaceRequest(namespace="x").adopt_existing is False
