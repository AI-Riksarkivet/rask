"""A route on the MANAGEMENT prefix is gated by the same guard as one on the spec prefix ([[LH-021]]).

FOUND BY SHIPPING THE FIRST ONE. `catalog.api.fga_deps._resource_for` and `_suffix` both matched
`/v1/` literally, so `/management/v1/table/{id}/erasure` resolved to NO resource, fell out of
`authorize` entirely, and was reachable by any authenticated principal — on the verb that destroys a
table's history. `test_stores_reads_are_gated.py` caught it before it shipped; these hold the
underlying parser so the NEXT management route does not rediscover it.

THE PARSER IS THE SUBJECT, not the one route. LH-021 moves 42 rask-only operations off the spec
surface, and every one of them will arrive through these two functions. A gate on the erasure route
alone would pass while the forty-second regressed.
"""

from __future__ import annotations

import pytest
from lance_namespace import InternalError

from catalog.api.fga_deps import _action_relation, _suffix


def test_erasure_clears_the_OWNER_rung() -> None:
    """It removes a subject from every ref, drops the tags pinning the versions that held them, and
    reclaims history. `can_drop` is the highest rung there is."""
    assert _action_relation("table", "erasure") == "can_drop"


def test_a_suffix_the_parser_cannot_strip_is_refused() -> None:
    """The RESOLVER half: `_suffix` yields `""` for a path no mount strips, and `""` names no door, so
    `_action_relation` refuses it rather than handing out a rung nobody chose.

    The guard never makes this call for such a path: `_resource_for` answers `None` for an unrecognised
    mount and `authorize` stops at authentication, which
    `test_stores_reads_are_gated.py::test_every_route_the_router_guard_waves_through_has_a_reason` covers."""
    assert _suffix("/some/other/mount/table/x/erasure", "table", "x") == ""
    with pytest.raises(InternalError, match="declares no rung"):
        _action_relation("table", "")
