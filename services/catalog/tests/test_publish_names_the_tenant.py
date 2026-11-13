"""`table_published` must name the tenant, because it is the only party that can.

A consumer of the event needs a project to route the next tier's work and to stamp `rask_lance.project`, which is WATCH
targeting's only key. A table's name begins with its warehouse ([[LH-593]]), and a warehouse is not its project: one
project holds several warehouses, and `CONTROL_ID_PATTERN` permits `-` inside either id, so no string rule recovers the
project from the name. The catalog holds the warehouse record that answers it, and already resolves exactly this for the
lineage facet, so it stamps the answer instead of leaving every consumer to guess, and the two provenance records this
one publication produces agree on the tenant.
"""

from __future__ import annotations

from typing import Any

import pytest


class _Emitter:
    """The control emitter, capturing what would be published."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event.model_dump() if hasattr(event, "model_dump") else event)


class _Lineage:
    """The lineage emitter seam — only `project_for` is exercised here."""

    def __init__(self, *, project: str | None) -> None:
        self._project = project
        self.asked: list[str] = []

    async def project_for(self, warehouse_id: str) -> str | None:
        self.asked.append(warehouse_id)
        return self._project


class TestWhenTheTenantCannotBeEstablished:
    @pytest.mark.asyncio
    async def test_the_publish_survives_that_outage_with_no_tenant(self) -> None:
        from catalog.api.v1.endpoints.publication import publication_extra
        from catalog.core.lineage_emit import _BaseLineageEmitter

        async def _down(warehouse_id: str) -> str | None:
            raise RuntimeError("registry unreachable")

        emitter = _BaseLineageEmitter()
        emitter._project_resolver = _down

        extra = await publication_extra(emitter, ["acme-bronze", "pages"], from_version=1, to_version=2, location="s3://b/t")

        assert "project" not in extra
        assert extra["to_version"] == 2


@pytest.mark.asyncio
async def test_the_stamp_is_the_project_of_the_warehouse_the_name_begins_with() -> None:
    from catalog.api.v1.endpoints.publication import publication_extra

    lineage = _Lineage(project="acme")

    extra = await publication_extra(lineage, ["acme-eu", "bronze", "pages"], from_version=1, to_version=2, location="s3://b/t")

    assert (extra["project"], lineage.asked) == ("acme", ["acme-eu"])
