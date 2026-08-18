"""`table_published` must name the tenant, because it is the only party that can.

The medallion's `/publication-arrival` head needs a project to route the cascade and to stamp
`lance.project`, which is WATCH targeting's only key. It had no project on the event, so it derived
one by taking the first segment of the table id — and for `acme-bronze$pages` that is `acme-bronze`,
the qualified NAMESPACE, not the project.

No fix on that side can be sound. `project_namespace` joins with `-` and `PROJECT_PATTERN` permits `-`
inside a project id, so `acme-bronze` is genuinely ambiguous between project `acme` and project
`acme-bronze` (`warehouses.project_for_namespace` says so, and `lineage_emit`'s `ProjectResolver` note
says it again). The catalog is the only component holding the registry binding that answers it, and it
already resolves exactly this for the lineage facet — through a cache, on the same request.

So it stamps the answer instead of leaving every consumer to guess, and the two provenance records
this one publication produces agree on the tenant.
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

    async def project_for(self, top_ns: str) -> str | None:
        self.asked.append(top_ns)
        return self._project


class TestTheEventNamesTheTenant:
    @pytest.mark.asyncio
    async def test_the_resolved_project_is_carried(self) -> None:
        from catalog.api.v1.endpoints.publication import publication_extra

        lineage = _Lineage(project="acme")

        extra = await publication_extra(lineage, ["acme-bronze", "pages"], from_version=3, to_version=7, location="s3://b/t")

        assert extra["project"] == "acme"

    @pytest.mark.asyncio
    async def test_it_is_resolved_from_the_TOP_namespace(self) -> None:
        """The binding is keyed by the top-level namespace, which is segment 0 — the one thing segment 0
        genuinely is."""
        from catalog.api.v1.endpoints.publication import publication_extra

        lineage = _Lineage(project="acme")

        await publication_extra(lineage, ["acme-bronze", "pages"], from_version=3, to_version=7, location="s3://b/t")

        assert lineage.asked == ["acme-bronze"]

    @pytest.mark.asyncio
    async def test_the_range_and_location_are_unchanged(self) -> None:
        """Additive. A consumer keying on the existing fields keeps working."""
        from catalog.api.v1.endpoints.publication import publication_extra

        extra = await publication_extra(_Lineage(project="acme"), ["acme-bronze", "pages"], from_version=3, to_version=7, location="s3://b/t")

        assert extra["from_version"] == 3
        assert extra["to_version"] == 7
        assert extra["location"] == "s3://b/t"


class TestWhenTheTenantCannotBeEstablished:
    @pytest.mark.asyncio
    async def test_an_unbound_namespace_carries_NO_project_key(self) -> None:
        """A single-tenant estate has no project, and an empty string is not one. Omitting it lets the
        consumer tell "no tenant" from "a tenant named ''"."""
        from catalog.api.v1.endpoints.publication import publication_extra

        extra = await publication_extra(_Lineage(project=None), ["bronze", "events"], from_version=1, to_version=2, location="s3://b/t")

        assert "project" not in extra

    @pytest.mark.asyncio
    async def test_a_registry_OUTAGE_reaches_the_caller_as_None_not_as_a_raise(self) -> None:
        """The contract `publication_extra` leans on instead of adding a swallow of its own. It runs
        after the tag has moved, so a raise here would report failure for committed work."""
        from catalog.core.lineage_emit import _BaseLineageEmitter

        async def _down(top_ns: str) -> str | None:
            raise RuntimeError("registry unreachable")

        emitter = _BaseLineageEmitter()
        emitter._project_resolver = _down

        assert await emitter.project_for("acme-bronze") is None

    @pytest.mark.asyncio
    async def test_the_publish_survives_that_outage_with_no_tenant(self) -> None:
        from catalog.api.v1.endpoints.publication import publication_extra
        from catalog.core.lineage_emit import _BaseLineageEmitter

        async def _down(top_ns: str) -> str | None:
            raise RuntimeError("registry unreachable")

        emitter = _BaseLineageEmitter()
        emitter._project_resolver = _down

        extra = await publication_extra(emitter, ["acme-bronze", "pages"], from_version=1, to_version=2, location="s3://b/t")

        assert "project" not in extra
        assert extra["to_version"] == 2
