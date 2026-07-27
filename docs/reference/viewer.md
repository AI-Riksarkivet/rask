# API Reference — viewer

!!! warning "Superseded (June 2026)"
    The `viewer` deployable described here no longer exists. The viewer monolith
    was dissolved into a gateway + per-domain services over a shared `core` brick;
    the API endpoints now live in `core-api` (batches/chunks/catalog) and
    `orchestrator`, fronted by the gateway on :8888. See
    `docs/architecture/microservices.md`. The text below is retained for
    historical reference.

This page was auto-generated from source docstrings in the old
`components/services/viewer/src` tree. That `viewer` Python package has been
**deleted from the repo**, so the auto-generated API sections below it are gone
too (mkdocstrings can no longer collect them). For the endpoint and service
breakdown of the successor fleet see
[Components → Services](../components/services.md); the code now lives in the
`core` package (`services/core`).
