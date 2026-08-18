"""`project_namespace` composes `<project>-<tier>`; only HALF of that is safely recoverable.

The estate's canon rules the project half out (`catalog/core/lineage_emit.py`, the `ProjectResolver`
note): `PROJECT_PATTERN` permits hyphens, so `acme-bronze-silver` is genuinely ambiguous between
project `acme` and project `acme-bronze`, "and guessing wrong notifies the wrong tenant's watchers.
The registry binding is the only sound answer."

The TIER half carries no such ambiguity — `GOVERNED_TIERS` is a closed vocabulary — so membership is a
fact. `namespace_tiers` answers that and nothing else. It exists because the annotator's publish door
asked the question of the whole STRING (`namespace in {"silver","gold"}`) and therefore never fired for
any namespace the estate actually has.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.warehouse_registry import namespace_tiers, project_namespace


class TestItRecoversTheTierWhateverTheProject:
    @pytest.mark.parametrize("project", ["", "acme", "my-cool-project", "a-b-c-d"])
    @pytest.mark.parametrize("tier", ["bronze", "silver", "gold"])
    def test_a_composed_namespace_yields_its_tier(self, project: str, tier: str) -> None:
        assert namespace_tiers(project_namespace(project, tier)) == frozenset({tier})

    @pytest.mark.parametrize(("lane", "tier"), [("bronze-media", "bronze"), ("silver-media", "silver"), ("gold-htr", "gold")])
    def test_a_LANE_still_yields_its_tier(self, lane: str, tier: str) -> None:
        """The cascade names lanes `<tier>-<lane>`. Reading such a name from the RIGHT yields the lane
        instead — the failure `maintenance/services/tiers.py` documents hitting live on `bronze-pages`."""
        assert namespace_tiers(project_namespace("acme", lane)) == frozenset({tier})


class TestItRefusesToPickWhenTheNameIsAMBIGUOUS:
    def test_two_tier_segments_return_BOTH(self) -> None:
        """`acme-bronze-gold` is either project `acme` with a `bronze-gold` lane, or project
        `acme-bronze` promoting into gold. Returning a set says so; picking one would let a caller
        gating on `& {"silver","gold"}` skip a door it must cross."""
        assert namespace_tiers("acme-bronze-gold") == frozenset({"bronze", "gold"})

    def test_an_authorization_gate_therefore_fails_CLOSED(self) -> None:
        """The property the annotator's publish door depends on."""
        assert namespace_tiers("acme-bronze-gold") & frozenset({"silver", "gold"})


class TestWhatIsNotATier:
    @pytest.mark.parametrize("name", ["acme", "scratch", "acme-scratch", "warehouse-metadata", ""])
    def test_a_name_with_no_tier_segment_yields_nothing(self, name: str) -> None:
        assert namespace_tiers(name) == frozenset()

    @pytest.mark.parametrize("name", ["goldfish", "silverware", "bronzed", "acme-goldfish"])
    def test_matching_is_SEGMENT_wise_not_substring(self, name: str) -> None:
        """A substring test would gate `goldfish`, demanding the validator rung from namespaces the
        cascade never touches."""
        assert namespace_tiers(name) == frozenset()

    def test_RAW_is_not_a_governed_tier(self) -> None:
        """R23: raw is the external world, deliberately outside the medallion — `GOVERNED_TIERS` is
        exactly bronze/silver/gold, and this reads that tuple rather than restating it."""
        assert namespace_tiers("acme-raw") == frozenset()
