"""Door 3 of the publish crossing never fires, because it tests a NAME where it means a TIER.

`_authorize_publish` crosses three doors: `can_publish` on the annotation project, `can_create_table`
on the target namespace, and — when the target is a validator-gated medallion stage — `can_promote` on
that namespace. The third is the whole point of the `validator` rung, which the FGA model defines so
that "a plain writer can write within a stage but cannot promote INTO a gated stage".

The gate is `namespace in {"silver", "gold"}`, an exact-string membership test. Every namespace the
estate actually has is project-QUALIFIED (`scripts/seed_estate.py` creates `acme-silver`, `acme-gold`),
so the test is False for all of them and door 3 is skipped. `target_namespace` is caller-supplied in
the request body, so this is reachable by naming the namespace you would have to name anyway — not a
crafted input, the normal path.

The fix is not a longer literal set. `acme-gold-htr` is a lane and must gate too, and a project may
itself contain hyphens, so "is this a gated tier" has to be asked of the TIER rather than of the whole
string. That question is sound to answer by splitting, because the tier vocabulary is closed
(`GOVERNED_TIERS`) — unlike the sibling question "which project is this", which the estate's canon at
`catalog/core/lineage_emit.py:306` rules cannot be a string split at all.
"""

from __future__ import annotations

import pytest
from annotator.api.v1.endpoints.project_events import _authorize_publish

from service_kit.exceptions import ForbiddenError


class _Checker:
    """Records every (relation, object) asked and allows everything, so a MISSING door shows up as an
    absent question rather than as an allowed request."""

    def __init__(self, *, deny: set[tuple[str, str]] | None = None) -> None:
        self.asked: list[tuple[str, str]] = []
        self._deny = deny or set()

    async def __call__(self, *, user: str, relation: str, obj: str) -> bool:
        self.asked.append((relation, obj))
        return (relation, obj) not in self._deny


async def _doors(namespace: str, *, deny: set[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    checker = _Checker(deny=deny)
    await _authorize_publish(checker, "user:mallory", "labels-2026", namespace)
    return checker.asked


class TestTheValidatorDoorFiresForEveryGatedTIER:
    @pytest.mark.parametrize(
        "namespace",
        [
            "silver",  # the single-tenant form — the ONLY shape that ever worked
            "gold",
            "acme-silver",  # what scripts/seed_estate.py actually creates
            "acme-gold",
            "acme-gold-htr",  # a lane: `<project>-<tier>-<lane>`
            "acme-silver-media",
            "my-cool-project-gold",  # PROJECT_PATTERN permits hyphens
        ],
    )
    @pytest.mark.asyncio
    async def test_can_promote_is_asked(self, namespace: str) -> None:
        asked = await _doors(namespace)

        assert ("can_promote", f"namespace:{namespace}") in asked, (
            f"publishing into {namespace!r} crossed no validator door — a writer who is not a validator "
            f"could promote into a gated stage, the exact semantics the rung exists to prevent"
        )

    @pytest.mark.asyncio
    async def test_a_non_validator_is_REFUSED_on_a_qualified_namespace(self) -> None:
        """The behavioural half: the door must not merely be asked, it must be able to close."""
        with pytest.raises(ForbiddenError):
            await _doors("acme-gold", deny={("can_promote", "namespace:acme-gold")})


class TestWhatIsNOTValidatorGated:
    @pytest.mark.parametrize("namespace", ["bronze", "acme-bronze", "acme-bronze-media"])
    @pytest.mark.asyncio
    async def test_bronze_crosses_only_the_writer_door(self, namespace: str) -> None:
        """Bronze is the first governed tier, not a promotion target. Gating it would demand the
        validator rung for an ordinary ingest write."""
        asked = await _doors(namespace)

        assert ("can_promote", f"namespace:{namespace}") not in asked

    @pytest.mark.parametrize("namespace", ["scratch", "acme-scratch", "acme"])
    @pytest.mark.asyncio
    async def test_a_namespace_that_is_no_TIER_is_not_gated(self, namespace: str) -> None:
        """A name carrying no tier segment is not a medallion stage, and inventing one out of a hyphen
        would demand the validator rung for namespaces the cascade never touches."""
        asked = await _doors(namespace)

        assert ("can_promote", f"namespace:{namespace}") not in asked


class TestTheOrderIsUnchanged:
    @pytest.mark.asyncio
    async def test_the_doors_are_crossed_in_order(self) -> None:
        """The audit trail must name the FIRST door that closed, not a composite verdict."""
        asked = await _doors("acme-gold")

        assert asked == [
            ("can_publish", "annotation_project:labels-2026"),
            ("can_create_table", "namespace:acme-gold"),
            ("can_promote", "namespace:acme-gold"),
        ]
