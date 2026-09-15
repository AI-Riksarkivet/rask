"""Every `/maintenance/` verb the catalog serves clears the owner bar, derived from the ROUTES.

[[LH-105]]. `fga_deps._action_relation` falls through to `can_write_data` for any table suffix it does
not recognise, so a door is owner-gated by being LISTED — never by what it does. That fall-through has
shipped two holes already, both recorded in `_OWNER_SUFFIX_RELATION`'s own comments: `branches/delete`
let a non-owner destroy a branch while the same caller was refused the lesser `tags/delete`, and
`tags/delete` let a plain writer remove the pin that `published` is.

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

from catalog.api.fga_deps import _OWNER_SUFFIX_RELATION, _action_relation


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
def test_a_maintenance_verb_is_mapped_rather_than_falling_through(suffix: str) -> None:
    """An unmapped suffix is not an omission with a safe default — it is the writer rung."""
    assert suffix in _OWNER_SUFFIX_RELATION["table"], (
        f"`{suffix}` is served but unmapped, so `_action_relation` answers {_action_relation('table', suffix)!r} for it — the writer rung, not the owner bar"
    )


@pytest.mark.parametrize("suffix", _maintenance_suffixes())
def test_a_maintenance_verb_clears_the_owner_bar(suffix: str) -> None:
    """Asserted through the resolver, not the dict: the mapping is only load-bearing if it is consulted."""
    assert _action_relation("table", suffix) == OWNER_RELATION


def test_an_unmapped_maintenance_verb_would_reach_the_writer_rung() -> None:
    """The premise the suite above rests on, stated as a test so it cannot quietly stop being true.

    If the fall-through were ever changed to something owner-tier, every assertion above would pass
    for a door nobody had mapped, and this suite would be proving nothing.
    """
    assert _action_relation("table", "maintenance/a-verb-nobody-mapped") == "can_write_data"
