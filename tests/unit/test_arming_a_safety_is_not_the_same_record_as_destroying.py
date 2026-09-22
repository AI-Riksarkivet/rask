"""The protection door must not audit under the name of the destruction it guards.

[[LH-055]]. `#41` audits every authorization decision, and `fga_deps._require` passes the RELATION as
the audit action: `audit(relation, ALLOW|DENY, subject=..., resource=...)`. The route suffix is not in
that record. So while `"protection"` mapped to `can_drop`, a caller arming a safety on a table and a
caller destroying it wrote identical audit lines — two opposite intentions, one trail, no way to tell
them apart after the fact.

THE FIX IS A NAME, NOT A TIER. `can_set_protection` resolves to `owner`, exactly as `can_drop` and
`can_delete` do, so nobody gained or lost a permission: it is a computed userset, which every existing
owner reaches the moment it exists. That is what distinguishes it from the `branches/delete` case,
which deliberately reuses `can_drop` rather than minting a rung "every existing owner grant would then
lack" — true of a grantable relation, false of a computed one.

The TIER identity is pinned where behaviour is pinned, in `model.fga.yaml` ("arming deletion protection
is its own ACTION at the drop's tier"), against the real evaluator. This file pins the other half: that
the door still names its own action.
"""

from __future__ import annotations

import pytest

from catalog.api.fga_deps import _OWNER_SUFFIX_RELATION, _action_relation


#: `(fga type, the destructive relation whose tier protection shares on that type)`.
_GUARDED: dict[str, str] = {"table": "can_drop", "namespace": "can_delete"}


@pytest.mark.parametrize(("fga_type", "destruction"), sorted(_GUARDED.items()))
def test_the_protection_door_checks_its_own_relation(fga_type: str, destruction: str) -> None:
    relation = _action_relation(fga_type, "protection")
    assert relation != destruction, (
        f"the {fga_type} protection door checks {relation!r}, the same relation as the destruction it "
        f"guards — so `#41`'s audit trail records arming a safety and destroying the object under one "
        f"action name, and the route suffix is not in the record to tell them apart."
    )
    assert relation == "can_set_protection", f"unexpected protection relation on {fga_type}: {relation!r}"


@pytest.mark.parametrize(("fga_type", "destruction"), sorted(_GUARDED.items()))
def test_the_destruction_it_guards_still_maps_to_the_destructive_relation(fga_type: str, destruction: str) -> None:
    """The control. Without it the test above passes on a map that stopped gating destruction at all."""
    # Both types spell their own destruction `drop` — the relation it maps to is what differs.
    assert _OWNER_SUFFIX_RELATION[fga_type]["drop"] == destruction, (
        f"{fga_type}'s own destruction no longer maps to {destruction!r} — the assertion above is then comparing against nothing."
    )
