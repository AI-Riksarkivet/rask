"""Every `/maintenance/` verb the catalog serves clears the owner bar, derived from the ROUTES.

[[LH-105]]. `fga_deps._action_relation` gates a door by the map that LISTS it — never by what it does —
and refuses a suffix no map declares. So a destructive verb listed in the writer map instead of the
owner map is the hole left to close here: `_OWNER_SUFFIX_RELATION`'s own comments record two verbs that
reached the writer rung, `branches/delete` letting a non-owner destroy a branch while the same caller
was refused the lesser `tags/delete`, and `tags/delete` letting a plain writer remove the pin that
`published` is.

WHY THE SUFFIXES COME OFF THE APP RATHER THAN A LIST. A test naming the doors it knows about cannot
fail for the door somebody adds next — which is the only failure that matters here, since every hole
so far was a NEW verb rather than a changed one. So the routes are read from the mounted application:
a `/maintenance/<verb>` added without a mapping fails here at the moment it is added.

`/maintenance/` specifically, rather than every table suffix, because this sub-path is where the
catalog's non-spec composite verbs live. They share one property that makes the rule stateable: each
either destroys version history, rewrites fragments, or replaces an index, so `can_drop` is the floor
for all of them and a cheaper answer is always wrong.
"""

from __future__ import annotations

import pytest
from lance_namespace import InternalError

from catalog.api.fga_deps import _action_relation


OWNER_RELATION = "can_drop"


def _maintenance_suffixes() -> list[str]:
    """Every `/v1/table/{id}/maintenance/<verb>` POST the catalog mounts, as `_action_relation` sees it."""
    from catalog.api.v1.endpoints import maintenance

    suffixes = []
    for route in maintenance.router.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set())
        marker = "/{id}/"
        if "POST" not in methods or marker not in path:
            continue
        suffix = path.split(marker, 1)[1]
        if suffix.startswith("maintenance/"):
            suffixes.append(suffix)
    return sorted(set(suffixes))


def test_the_router_actually_serves_maintenance_verbs() -> None:
    """Without this the parametrized suite below would pass by iterating nothing."""
    assert len(_maintenance_suffixes()) >= 3, f"expected the preview/run/compact family at minimum, found {_maintenance_suffixes()}"


@pytest.mark.parametrize("suffix", _maintenance_suffixes())
def test_a_maintenance_verb_clears_the_owner_bar(suffix: str) -> None:
    """Asserted through the resolver, not the dict: the mapping is only load-bearing if it is consulted."""
    assert _action_relation("table", suffix) == OWNER_RELATION


def test_an_unmapped_maintenance_verb_is_refused_rather_than_owner_gated() -> None:
    """The premise the suite above rests on, stated as a test so it cannot quietly stop being true.

    If an undeclared suffix ever resolved to something owner-tier, every assertion above would pass for
    a door nobody had mapped, and this suite would be proving nothing.
    """
    with pytest.raises(InternalError, match="declares no rung"):
        _action_relation("table", "maintenance/a-verb-nobody-mapped")
