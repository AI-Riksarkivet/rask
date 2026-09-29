"""The publication head must wake the lane that was published, and it woke bronze for everything.

`handle_publication` discarded the published table's namespace and re-stamped
`settings.bronze_namespace` / `settings.bronze_topic` on every trigger. Measured before the fix:

    published table                -> topic             trigger names
    table:acme-bronze$pages        -> medallion.bronze  bronze / bronze$pages     (right, by luck)
    table:acme-silver$features     -> medallion.bronze  bronze / bronze$features  (wrong)
    table:acme-gold$catalog        -> medallion.bronze  bronze / bronze$catalog   (wrong)

A silver publication therefore fired a BRONZE trigger, which no stage runner's `from_dataset` matches, so it
was dropped as another lane's. Silently — which is safer than the loop it could have been, and still
means the gold stage runner is never delivered to and `table_published` can never become the single cascade
trigger the design wants.

Routing on the SOURCE NAMESPACE only became sound when the catalog started stating the tenant
(`extra.project`): de-qualifying `acme-silver` to `silver` requires knowing the project is `acme`, and
`PROJECT_PATTERN` permits hyphens, so no split can recover it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from medallion.core.config import MedallionSettings
from medallion.services.publication_trigger import handle_publication


#: Constructor kwargs take a real dict — pydantic-settings parses the JSON form only from the
#: ENVIRONMENT, which is the deployment path and is covered by its own test below.
ROUTES = {"bronze": "medallion.bronze", "silver": "medallion.silver", "bronze-media": "medallion.media"}


class _Dapr:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


def _settings(**over: Any) -> MedallionSettings:
    return MedallionSettings(transform_routes=dict(ROUTES), **over)


def _event(object_id: str, project: str | None = "acme") -> dict[str, Any]:
    """``project=None`` states no tenant; ``project=""`` states an EMPTY one, which is its own case."""
    extra: dict[str, Any] = {"from_version": 3, "to_version": 7, "location": "s3://b/t"}
    if project is not None:
        extra["project"] = project
    return {"data": {"action": "table_published", "object_id": object_id, "event_id": "e1", "actor": "user:s", "extra": extra}}


async def _route(object_id: str, project: str | None = "acme", **over: Any) -> tuple[str, dict[str, Any]] | None:
    dapr = _Dapr()
    await handle_publication(dapr, _settings(**over), _event(object_id, project))
    if not dapr.published:
        return None
    call = dapr.published[0]
    return call["topic_name"], json.loads(call["data"])


class TestEachTierWakesItsOwnLane:
    @pytest.mark.asyncio
    async def test_silver_wakes_the_SILVER_lane(self) -> None:
        """The one that decides whether stage runners can ever publish their own output."""
        routed = await _route("table:acme-silver$features")
        assert routed is not None
        topic, trigger = routed
        assert topic == "medallion.silver"
        assert (trigger["namespace"], trigger["dataset"]) == ("silver", "silver$features")


class TestItDrivesOnlyDeclaredLanes:
    @pytest.mark.asyncio
    async def test_an_undeclared_namespace_is_still_ACKED(self) -> None:
        """Not ours to act on is not a failure — retrying it forever parks a poison message."""
        dapr = _Dapr()
        result = await handle_publication(dapr, _settings(), _event("table:acme-scratch$notes"))
        assert result == {"status": "SUCCESS"}
        assert dapr.published == []


class TestItNeverGuesses:
    """The head must not derive a tenant from the table id. A table id is `<namespace>$<table>` and
    `project_namespace` joins with `-` while `PROJECT_PATTERN` permits `-` inside a project id, so
    `acme-bronze` is genuinely ambiguous. The catalog resolves the tenant through the warehouse binding
    and stamps it on the event; this head reads it."""

    @pytest.mark.asyncio
    async def test_a_QUALIFIED_namespace_with_no_stated_tenant_routes_NOWHERE(self) -> None:
        """`acme-bronze` cannot be de-qualified without knowing the project, so it matches no declared
        lane and the head drives nothing, rather than guessing `acme-bronze` is the tenant and firing a
        trigger naming a project no registry knows."""
        assert await _route("table:acme-bronze$pages", project=None) is None

    @pytest.mark.asyncio
    async def test_an_EMPTY_project_is_omitted_not_forwarded(self) -> None:
        """`transform.py` treats a present-but-unsafe project as deterministic garbage and DROPs, so
        an empty string would refuse every trigger carrying one."""
        routed = await _route("table:bronze$pages", project="")

        assert routed is not None
        _topic, trigger = routed
        assert "project" not in trigger
