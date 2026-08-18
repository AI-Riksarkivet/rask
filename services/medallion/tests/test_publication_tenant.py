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
    return MedallionSettings(MEDALLION_BRONZE_NAMESPACE="bronze", MEDALLION_BRONZE_TOPIC="medallion.bronze")  # type: ignore[arg-type]


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
    async def test_an_event_with_NO_project_carries_none(self) -> None:
        """The single-tenant estate, and the pre-fix catalog during a rolling deploy. Deriving one from
        `acme-bronze` is what produced a tenant that does not exist."""
        trigger = await _trigger("table:acme-bronze$pages")

        assert trigger is not None
        assert "project" not in trigger, f"the head invented a tenant: {trigger.get('project')!r}"

    @pytest.mark.asyncio
    async def test_an_EMPTY_project_is_omitted_not_forwarded(self) -> None:
        """`transform.py` treats a present-but-unsafe project as deterministic garbage and DROPs, so
        an empty string would refuse every trigger carrying one."""
        trigger = await _trigger("table:acme-bronze$pages", {"project": ""})

        assert trigger is not None
        assert "project" not in trigger

    @pytest.mark.asyncio
    async def test_a_nested_identifier_is_not_split_either(self) -> None:
        """The shape the old docstring was written for. It is not produced by any door, and honouring
        it here would keep one guess alive for the sake of a shape that never arrives."""
        trigger = await _trigger("table:acme$bronze$pages")

        assert trigger is not None
        assert "project" not in trigger


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
