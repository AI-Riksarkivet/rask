"""Every `/maintenance/` verb the catalog serves clears the owner bar, derived from the ROUTES.

[[LH-105]]. `fga_deps._action_relation` gates a door by the map that LISTS it — never by what it does —
and refuses a suffix no map declares. So a destructive verb listed in the writer map instead of the
owner map is the hole left to close here: `_OWNER_SUFFIX_RELATION`'s own comments record two verbs that
reached the writer rung, `branches/delete` letting a non-owner destroy a branch while the same caller
was refused the lesser `tags/delete`, and `tags/delete` letting a plain writer remove the pin that
`published` is.

WHY THE SUFFIXES COME OFF THE APP RATHER THAN A LIST. A test naming the doors it knows about cannot
fail for the door somebody adds next — which is the only failure that matters here, since every hole
so far was a NEW verb rather than a changed one. So the routes are read off the composed application
through `router_walk.mounted()`, which sees through the included routers a walk of `router.routes` finds
nothing in: a `/maintenance/<verb>` added without a mapping fails here at the moment it is added, and a
walk that finds no verb fails collection (`empty_parameter_set_mark` in the root `pyproject.toml`).

`/maintenance/` specifically, rather than every table suffix, because this sub-path is where the
catalog's non-spec composite verbs live. They share one property that makes the rule stateable: each
either destroys version history, rewrites fragments, or replaces an index, so `can_drop` is the floor
for all of them and a cheaper answer is always wrong. The two maintainer doors are the stated exception
(ADR 0204, [[LH-575]]): a disarm removes the keys that would let a commit delete versions, and a floor raise
commits a stamp that changes no row, so they sit at the maintainer rung the sweep holds, and every verb added
next still meets the owner bar.
"""

from __future__ import annotations

import pytest
import router_walk

from catalog.api.fga_deps import _action_relation


OWNER_RELATION = "can_drop"
#: The maintainer doors: neither destroys history, rewrites a fragment nor replaces an index.
MAINTAINER_DOORS = {"maintenance/disarm": "can_maintain", "maintenance/floor": "can_maintain"}


def _maintenance_suffixes() -> list[str]:
    """Every `/v1/table/{id}/maintenance/<verb>` POST the catalog mounts, as `_action_relation` sees it."""
    marker = "/{id}/"
    suffixes = {path.split(marker, 1)[1] for path, route in router_walk.mounted() if "POST" in (route.methods or ()) and marker in path}
    return sorted(suffix for suffix in suffixes if suffix.startswith("maintenance/"))


@pytest.mark.parametrize("suffix", _maintenance_suffixes())
def test_a_maintenance_verb_clears_the_owner_bar_and_a_maintainer_door_the_maintainer_rung(suffix: str) -> None:
    """Asserted through the resolver, not the dict: the mapping is only load-bearing if it is consulted."""
    assert _action_relation("table", suffix) == MAINTAINER_DOORS.get(suffix, OWNER_RELATION)
