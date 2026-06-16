# API Reference — viewer

!!! warning "Superseded (June 2026)"
    The `viewer` deployable described here no longer exists. The viewer monolith
    was dissolved into a gateway + per-domain services over a shared `core` brick;
    the API endpoints now live in `core-api` (batches/chunks/catalog) and
    `orchestrator`, fronted by the gateway on :8888. See
    `docs/architecture/microservices.md`. The text below is retained for
    historical reference.

Auto-generated from source docstrings in `components/services/viewer/src`. For
the endpoint and service breakdown see [Components → Services](../components/services.md).

## Configuration

::: viewer.core.config

## Models

::: viewer.models.batch

::: viewer.models.pipelines

## Services

::: viewer.services.sync

::: viewer.services.submission

::: viewer.services.orchestrator.loop

::: viewer.services.orchestrator.derive

::: viewer.services.ray_dashboard

## Search & catalog

::: viewer.services.discover.search

::: viewer.services.discover.catalog
