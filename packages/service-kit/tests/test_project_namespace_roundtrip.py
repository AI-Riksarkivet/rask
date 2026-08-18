"""`project_namespace` had no inverse, so five callers each invented one — and one of them was wrong.

The convention is one line: a catalog namespace is `<project>-<tier>` (`acme-bronze`), and a table id
is `<namespace>$<table>` (`acme-bronze$pages`). `project_namespace` OWNS the composing half, and its
own docstring already records why it must be shared: the ingest plane once composed
`f"{project}${dataset}"` instead, so the cascade head could never fire on ingest's bronze writes.
"A naming convention that two services must agree on cannot live inside one of them."

The DECOMPOSING half was left to each caller. `publication_trigger._split_object_id` read the project
as the first `$`-segment, which for the estate's own seeded ids (`scripts/seed_estate.py:196-198`
creates `acme-bronze`, `acme-silver`, `acme-gold`) yields `acme-bronze` — the qualified NAMESPACE
wearing the project's name. That is not a near-miss either: it is a project no registry knows, so the
mover cannot resolve a warehouse root and DROPs, and the wrong `lance.project` zeroes the notification
watcher lane on the way past.

So the inverse lives here, beside the constructor, and the round-trip is the test.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.warehouse_registry import project_namespace, split_project_namespace


class TestTheRoundTrip:
    """Whatever `project_namespace` composes, this must take apart — that is the entire contract."""

    @pytest.mark.parametrize(
        ("project", "name"),
        [
            ("acme", "bronze"),
            ("acme", "silver"),
            ("acme", "gold"),
            # A LANE. The cascade runs several side by side and names each `<tier>-<lane>`, so the
            # qualified form has three segments and the tier is the MIDDLE one. Reducing from the
            # right yields the lane, which is the failure `maintenance/services/tiers.py` documents
            # hitting live on `bronze-pages`.
            ("acme", "bronze-media"),
            ("acme", "silver-media"),
            ("acme", "gold-htr"),
            # A HYPHENATED PROJECT. `PROJECT_PATTERN` permits hyphens, so the project is not simply
            # "the part before the first hyphen" — the tier is what marks the boundary.
            ("my-cool-project", "silver"),
            ("a-b-c-d", "gold"),
            # SINGLE-TENANT (#84): no project, name unchanged, and the inverse must say so rather
            # than inventing one.
            ("", "bronze"),
            ("", "gold-htr"),
        ],
    )
    def test_split_undoes_qualify(self, project: str, name: str) -> None:
        assert split_project_namespace(project_namespace(project, name)) == (project, name)


class TestTheShapesTheEstateActuallyProduces:
    """Read off `scripts/seed_estate.py`, which drives the real doors — not off a fixture."""

    @pytest.mark.parametrize(
        ("qualified", "expected"),
        [
            ("acme-bronze", ("acme", "bronze")),
            ("acme-silver", ("acme", "silver")),
            ("acme-gold", ("acme", "gold")),
        ],
    )
    def test_the_seeded_namespaces_decompose(self, qualified: str, expected: tuple[str, str]) -> None:
        assert split_project_namespace(qualified) == expected

    def test_a_projectless_namespace_yields_NO_project_not_a_guessed_one(self) -> None:
        """The single-tenant default. An empty project is omitted by every caller downstream; a
        guessed one is carried, and a carried wrong project is what silently drops the cascade."""
        assert split_project_namespace("bronze") == ("", "bronze")


class TestWhatItRefusesToGuess:
    def test_a_namespace_with_no_TIER_is_not_split(self) -> None:
        """The tier is the only marker of where a project ends. Without one there is no boundary to
        find, and splitting on a hyphen anyway would invent a project out of a plain name."""
        assert split_project_namespace("warehouse-metadata") == ("", "warehouse-metadata")
        assert split_project_namespace("acme") == ("", "acme")

    def test_an_empty_string_is_not_a_crash(self) -> None:
        assert split_project_namespace("") == ("", "")

    def test_the_FIRST_tier_segment_wins(self) -> None:
        """Deterministic rather than clever. A name carrying two tier words is already pathological;
        what matters is that both halves of the round-trip agree on which one bounds the project."""
        assert split_project_namespace("acme-bronze-gold") == ("acme", "bronze-gold")

    def test_a_leading_tier_segment_means_NO_project(self) -> None:
        """`bronze-media` is a lane, not project `bronze` with tier `media`. Reading it the other way
        is exactly the reduce-from-the-wrong-end bug `tiers.py` exists to prevent."""
        assert split_project_namespace("bronze-media") == ("", "bronze-media")
