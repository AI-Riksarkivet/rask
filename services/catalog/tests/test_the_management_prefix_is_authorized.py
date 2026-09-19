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

from catalog.api.fga_deps import _action_relation, _resource_for, _suffix


_ID = "acme-bronze$events"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/management/v1/table/{_ID}/erasure", "table"),
        (f"/v1/table/{_ID}/describe", "table"),
        (f"/management/v1/namespace/{_ID}/undrop", "namespace"),
    ],
)
def test_both_mounts_resolve_to_the_guarded_resource(path: str, expected: str) -> None:
    assert _resource_for(path) == expected


def test_an_unmounted_path_resolves_to_nothing(path: str = "/livez") -> None:
    """The control. `None` is what tells `authorize` this path is not a guarded resource at all, so a
    parser that matched everything would gate health probes and one that matched nothing would gate
    no route — the second being what actually happened."""
    assert _resource_for(path) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/management/v1/table/{_ID}/erasure", "erasure"),
        (f"/v1/table/{_ID}/maintenance/run", "maintenance/run"),
        (f"/management/v1/table/{_ID}/maintenance/run", "maintenance/run"),
    ],
)
def test_the_suffix_survives_either_mount(path: str, expected: str) -> None:
    """The suffix is what `_action_relation` looks up, so a mount it cannot strip silently collapses
    every owner-tier verb to the writer rung."""
    assert _suffix(path, "table", _ID) == expected


def test_erasure_clears_the_OWNER_rung() -> None:
    """It removes a subject from every ref, drops the tags pinning the versions that held them, and
    reclaims history. `can_drop` is the highest rung there is."""
    assert _action_relation("table", "erasure") == "can_drop"


def test_an_unstrippable_mount_would_be_caught_here() -> None:
    """The regression this file exists for, stated as a test rather than a comment: a suffix of `""`
    is what a route on an unrecognised mount produces, and `_action_relation` answers the WRITER rung
    for it — so the failure is not a refusal an operator would notice, it is a quieter permission."""
    assert _suffix("/some/other/mount/table/x/erasure", "table", "x") == ""
    assert _action_relation("table", "") != "can_drop"
