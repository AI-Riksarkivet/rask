"""Destroying a branch must clear the owner bar, like every other destructive door on a table.

`_OWNER_SUFFIX_RELATION["table"]` names the suffixes that demand an owner rung, and it lists
`branches/create` (`can_create_branch`), `tags/create`, `tags/update`, `tags/delete` (`can_drop`),
`version/delete` (`can_drop`), `drop`, `deregister`, `rename` and `restore`. **`branches/delete` is
not in it**, so `_action_relation` falls it through to the writer default `can_write_data`.

THE ASYMMETRY IS THE ARGUMENT, and it rests on what the estate already decided rather than on any claim
about recoverability. `spec.yaml:2195` says only "Delete an existing branch from table `id`" and does
not state what becomes of the branch's data, so nothing here asserts that it does. What IS settled: a
tag is a POINTER and its delete already demands `can_drop`, while `branches/create` FORKS HISTORY
(`fga_deps.py:149`, that module's own words), so a branch's versions do not sit on main's sequence.
Refusing a writer the pointer's delete while permitting the fork's is incoherent whichever way the
recoverability question resolves.

MEASURED ON THE DEPLOYED CATALOG 2026-09-11, with bob — a writer on `bronze$events` who holds no owner
rung there (he is refused 403 on `deregister` and on `protection`):

    POST /v1/table/bronze$events/branches/delete  -> 422   (cleared authz, failed body validation)
    POST /v1/table/bronze$events/tags/delete      -> 403
    POST /v1/table/bronze$events/deregister       -> 403

The 422 is the finding: a non-owner reached the endpoint. The route is real and reachable
(`endpoints/branches.py:59`), so this is not a guard for a door nothing arrives at — it is a door that
arrives at the wrong guard.

`can_drop` rather than a new relation: the rung already means "may destroy this table's contents", it
is what `tags/delete` and `version/delete` use, and inventing a `can_delete_branch` would add a rung
the model must define and every existing owner grant would then lack.
"""

from __future__ import annotations

from catalog.api import fga_deps


def test_branch_delete_demands_an_owner_rung() -> None:
    """THE GATE. Absent from the owner map, the suffix falls through to the writer default."""
    relation = fga_deps._action_relation("table", "branches/delete")

    assert relation != "can_write_data", (
        "deleting a branch resolved to the WRITER rung — measured live, a non-owner cleared this door "
        "(422) while being refused `tags/delete` and `deregister` (403)"
    )
    assert relation == "can_drop", f"a branch delete destroys data and history; it clears the same bar as a drop, not {relation!r}"


def test_it_matches_the_bar_its_lesser_sibling_already_clears() -> None:
    """A tag is a pointer, a branch is data — the pointer's delete must not be the stricter of the two."""
    assert fga_deps._action_relation("table", "tags/delete") == fga_deps._action_relation("table", "branches/delete")


def test_creating_a_branch_keeps_its_own_rung() -> None:
    """Unchanged: `branches/create` has a relation of its own because forking history is not destroying it.

    Pinned so the fix cannot be "mapped them both to can_drop" — create and delete are different acts
    and the model already separates them.
    """
    assert fga_deps._action_relation("table", "branches/create") == "can_create_branch"


def test_listing_branches_is_still_a_read() -> None:
    """The read half must not be dragged up with the destructive one — `branches/list` discloses names,
    which is metadata, and pinning it here stops a future edit from widening the whole prefix."""
    assert fga_deps._action_relation("table", "branches/list") == "can_get_metadata"
