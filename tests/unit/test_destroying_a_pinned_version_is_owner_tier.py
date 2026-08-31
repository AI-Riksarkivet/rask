"""Deleting a TAG or a tagged VERSION must clear the same bar as the maintenance that respects them.

The map's own comment states the rule that produced the hole: *"Their `*/delete` / `*/list` /
`*/version` siblings fall through to the reader/writer tiers below."* So `tags/create` and
`tags/update` are owner-gated (`can_create_tag`, `can_update_tag`) while `tags/delete` and
`version/delete` are not mapped at all and land on the writer rung.

That is backwards, and the asymmetry is what makes it exploitable rather than merely untidy:

- `maintenance/run` — which reclaims old versions and EXEMPTS tagged ones — is owner-gated
  (`can_drop`). The door that respects the tag is guarded.
- `version/delete` at writer tier destroys a tag-pinned version directly, leaving `published`
  pointing at bytes that no longer exist. The door that ignores the tag is not.
- `tags/delete` at writer tier removes the pin, which then defeats the owner-tier rollback guard:
  publication refuses to move `published` BACKWARDS, so a writer deletes the tag and republishes at
  an older version instead.

`published` is the estate's serving pointer — the whole point of the publish gate is that a consumer
reads a version something asserted about. A writer being able to unpin or destroy it makes the gate
advisory.

Note WHY this rung exists here and not in the catalog's database: Lance keeps the CAS in the object
store, so the pointer is a tag INSIDE the dataset rather than a row a catalog transaction owns. That
is the architecture working as designed — and it is exactly why the authorization has to carry the
weight the database is not carrying.
"""

from __future__ import annotations

import pytest

from catalog.api.fga_deps import _OWNER_SUFFIX_RELATION


TABLE = _OWNER_SUFFIX_RELATION["table"]


@pytest.mark.parametrize("suffix", ["tags/delete", "version/delete"])
def test_a_destructive_version_op_is_not_left_on_the_writer_rung(suffix: str) -> None:
    """An UNMAPPED suffix falls through to writer — which the map's own comments call out twice as
    'both wrong and silently wrong' for other routes, and it is the same failure here."""
    assert suffix in TABLE, (
        f"{suffix!r} is unmapped, so it falls through to the writer rung: a plain data writer can "
        "destroy or unpin the version `published` points at, while `maintenance/run` — which exempts "
        "tagged versions — is owner-gated"
    )


@pytest.mark.parametrize("suffix", ["tags/delete", "version/delete"])
def test_it_clears_the_same_bar_as_the_maintenance_that_respects_tags(suffix: str) -> None:
    """`maintenance/run` reclaims versions and EXEMPTS tagged ones, at `can_drop`. A door that
    destroys the same thing without the exemption cannot ask for less."""
    assert TABLE[suffix] == TABLE["maintenance/run"], (
        f"{suffix!r} is gated on {TABLE.get(suffix)!r} while the tag-respecting reclamation is gated "
        f"on {TABLE['maintenance/run']!r} — the unguarded door must not be the cheaper one"
    )


def test_the_tag_lifecycle_is_gated_end_to_end() -> None:
    """Create and update were already owner; delete completes the set. A lifecycle guarded at two of
    three points is guarded at none of them, because the ungated verb reaches the same state."""
    assert TABLE["tags/create"] == "can_create_tag"
    assert TABLE["tags/update"] == "can_update_tag"
    assert "tags/delete" in TABLE, "the tag lifecycle is gated on create and update but not delete"
