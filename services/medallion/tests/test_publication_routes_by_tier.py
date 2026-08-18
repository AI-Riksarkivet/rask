"""The publication head must wake the lane that was published, and it woke bronze for everything.

`handle_publication` discarded the published table's namespace and re-stamped
`settings.bronze_namespace` / `settings.bronze_topic` on every trigger. Measured before the fix:

    published table                -> topic             trigger names
    table:acme-bronze$pages        -> medallion.bronze  bronze / bronze$pages     (right, by luck)
    table:acme-silver$features     -> medallion.bronze  bronze / bronze$features  (wrong)
    table:acme-gold$catalog        -> medallion.bronze  bronze / bronze$catalog   (wrong)

A silver publication therefore fired a BRONZE trigger, which no mover's `from_dataset` matches, so it
was dropped as another lane's. Silently — which is safer than the loop it could have been, and still
means the gold mover is never delivered to and `table_published` can never become the single cascade
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
    return MedallionSettings(MEDALLION_LANE_ROUTES=dict(ROUTES), **over)  # type: ignore[arg-type]


def _event(object_id: str, project: str | None = "acme") -> dict[str, Any]:
    extra: dict[str, Any] = {"from_version": 3, "to_version": 7, "location": "s3://b/t"}
    if project:
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
    async def test_bronze_wakes_the_bronze_lane(self) -> None:
        routed = await _route("table:acme-bronze$pages")
        assert routed is not None
        topic, trigger = routed
        assert topic == "medallion.bronze"
        assert (trigger["namespace"], trigger["dataset"]) == ("bronze", "bronze$pages")

    @pytest.mark.asyncio
    async def test_silver_wakes_the_SILVER_lane(self) -> None:
        """The one that decides whether movers can ever publish their own output."""
        routed = await _route("table:acme-silver$features")
        assert routed is not None
        topic, trigger = routed
        assert topic == "medallion.silver"
        assert (trigger["namespace"], trigger["dataset"]) == ("silver", "silver$features")

    @pytest.mark.asyncio
    async def test_a_LANE_namespace_routes_to_its_own_topic(self) -> None:
        """`bronze-media` is a lane, not bronze. Reducing it to a tier would merge two cascades."""
        routed = await _route("table:acme-bronze-media$frames")
        assert routed is not None
        topic, trigger = routed
        assert topic == "medallion.media"
        assert trigger["namespace"] == "bronze-media"


class TestDeQualification:
    @pytest.mark.asyncio
    async def test_a_hyphenated_project_still_yields_the_bare_source(self) -> None:
        """`my-team-silver` with project `my-team` is `silver`. No split could do this — which is why
        the catalog states the project."""
        routed = await _route("table:my-team-silver$features", project="my-team")
        assert routed is not None
        assert routed[0] == "medallion.silver"

    @pytest.mark.asyncio
    async def test_a_projectless_estate_routes_on_the_bare_namespace(self) -> None:
        routed = await _route("table:silver$features", project=None)
        assert routed is not None
        assert routed[0] == "medallion.silver"


class TestItDrivesOnlyDeclaredLanes:
    @pytest.mark.asyncio
    async def test_an_UNDECLARED_namespace_publishes_no_trigger(self) -> None:
        """A table outside the cascade is published all the time. Waking bronze for it is what the old
        head did, and it is worse than silence: it fires compute for data no lane owns."""
        assert await _route("table:acme-scratch$notes") is None

    @pytest.mark.asyncio
    async def test_an_undeclared_namespace_is_still_ACKED(self) -> None:
        """Not ours to act on is not a failure — retrying it forever parks a poison message."""
        dapr = _Dapr()
        result = await handle_publication(dapr, _settings(), _event("table:acme-scratch$notes"))
        assert result == {"status": "SUCCESS"}
        assert dapr.published == []

    @pytest.mark.asyncio
    async def test_NO_routes_configured_drives_nothing(self) -> None:
        """A deployment that declares no lanes has no cascade to wake, and guessing bronze is the
        defect this replaces."""
        dapr = _Dapr()
        await handle_publication(dapr, MedallionSettings(), _event("table:acme-silver$features"))  # type: ignore[arg-type]
        assert dapr.published == []


class TestWhatIsUnchanged:
    @pytest.mark.asyncio
    async def test_the_range_and_tenant_still_ride(self) -> None:
        routed = await _route("table:acme-silver$features")
        assert routed is not None
        _, trigger = routed
        assert (trigger["from_version"], trigger["to_version"]) == (3, 7)
        assert trigger["project"] == "acme"


class TestTheDeploymentPath:
    def test_the_JSON_env_form_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The chart renders this as a JSON string. A dict works in a constructor and proves nothing
        about the path a pod actually takes."""
        monkeypatch.setenv("MEDALLION_LANE_ROUTES", json.dumps(ROUTES))

        assert MedallionSettings().lane_routes == ROUTES  # type: ignore[call-arg]
