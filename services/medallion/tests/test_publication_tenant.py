"""The publication head must not derive a tenant. It has no sound way to, and it was getting it wrong.

`_split_object_id` took the first `$`-segment of the table id as the project. For every namespace the
estate seeds (`scripts/seed_estate.py` creates `acme-bronze`/`acme-silver`/`acme-gold`, and a table id
is `<namespace>$<table>`) that is the qualified NAMESPACE, not the project — so the cascade got
`project="acme-bronze"`, a tenant no registry knows.

There is no better split. `project_namespace` joins with `-` and `PROJECT_PATTERN` permits `-` inside a
project id, so `acme-bronze` is genuinely ambiguous. The catalog resolves it through the warehouse
binding and now stamps it on the event; this head reads it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from medallion.core.config import MedallionSettings
from medallion.services.publication_trigger import handle_publication


class _Dapr:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


def _settings() -> MedallionSettings:
    # The head drives only DECLARED lanes now; without a route it publishes nothing at all.
    return MedallionSettings(transform_routes={"bronze": "medallion.bronze"})


def _event(object_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "data": {
            "action": "table_published",
            "object_id": object_id,
            "event_id": "evt-1",
            "actor": "user:CiQwOGE4",
            "extra": {"from_version": 3, "to_version": 7, "location": "s3://b/t", **(extra or {})},
        }
    }


async def _trigger(object_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    dapr = _Dapr()
    await handle_publication(dapr, _settings(), _event(object_id, extra))
    return json.loads(dapr.published[0]["data"]) if dapr.published else None


class TestTheTenantComesFromTheEvent:
    @pytest.mark.asyncio
    async def test_the_catalogs_project_is_carried_verbatim(self) -> None:
        trigger = await _trigger("table:acme-bronze$pages", {"project": "acme"})

        assert trigger is not None
        assert trigger["project"] == "acme"

    @pytest.mark.asyncio
    async def test_a_hyphenated_project_is_carried_verbatim(self) -> None:
        """The shape no split can recover: the namespace is `my-team-bronze`, and only the registry
        knows the project is `my-team` rather than `my` or `my-team-bronze`."""
        trigger = await _trigger("table:my-team-bronze$pages", {"project": "my-team"})

        assert trigger is not None
        assert trigger["project"] == "my-team"


class TestItNeverGuesses:
    @pytest.mark.asyncio
    async def test_a_projectless_estate_carries_no_tenant(self) -> None:
        """The single-tenant shape: the namespace is bare, so there is nothing to de-qualify and
        nothing to invent."""
        trigger = await _trigger("table:bronze$pages")

        assert trigger is not None
        assert "project" not in trigger, f"the head invented a tenant: {trigger.get('project')!r}"

    @pytest.mark.asyncio
    async def test_a_QUALIFIED_namespace_with_no_stated_tenant_routes_NOWHERE(self) -> None:
        """`acme-bronze` cannot be de-qualified without knowing the project, so it matches no declared
        lane and the head drives nothing. Stricter than the old behaviour, which guessed `acme-bronze`
        was the tenant and fired a trigger naming a project no registry knows."""
        assert await _trigger("table:acme-bronze$pages") is None

    @pytest.mark.asyncio
    async def test_an_EMPTY_project_is_omitted_not_forwarded(self) -> None:
        """`transform.py` treats a present-but-unsafe project as deterministic garbage and DROPs, so
        an empty string would refuse every trigger carrying one."""
        trigger = await _trigger("table:bronze$pages", {"project": ""})

        assert trigger is not None
        assert "project" not in trigger

    @pytest.mark.asyncio
    async def test_a_nested_identifier_routes_nowhere_rather_than_being_split(self) -> None:
        """The shape the old docstring was written for, produced by no door. Its namespace is
        `acme$bronze`, which matches no declared lane — so the head drives nothing rather than
        keeping a guess alive for a shape that never arrives."""
        assert await _trigger("table:acme$bronze$pages") is None


class TestTheLaneIsUnchanged:
    @pytest.mark.asyncio
    async def test_the_lane_is_still_the_tier_qualified_TABLE_name(self) -> None:
        """The half that was always right: the table name is the LAST segment, tier-qualified from the
        medallion's own setting so it reads the same for every tenant."""
        trigger = await _trigger("table:acme-bronze$pages", {"project": "acme"})

        assert trigger is not None
        assert trigger["dataset"] == "bronze$pages"
        assert trigger["namespace"] == "bronze"

    @pytest.mark.asyncio
    async def test_the_range_still_rides_along(self) -> None:
        trigger = await _trigger("table:acme-bronze$pages", {"project": "acme"})

        assert trigger is not None
        assert (trigger["from_version"], trigger["to_version"]) == (3, 7)
