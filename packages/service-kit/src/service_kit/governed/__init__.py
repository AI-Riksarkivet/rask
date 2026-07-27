"""The governed layer — auth/FGA/audit infrastructure shared by every lance-ns service.

These modules (``secrets``, ``dapr_auth``, ``fga``, ``oidc``, ``audit``, ``user_state``) are FastAPI-free
(or framework-light) infrastructure imported by the catalog, lineage, medallion and compaction services
alike. Extracted from the catalog's ``core/`` so no service depends on another service's package: every
service imports the shared pieces from ``service_kit``, and ``service_kit`` itself depends on none of the
services.

Requires the ``service-kit[governed]`` extra (openfga-sdk, pyjwt, aiohttp, tenacity, httpx,
lance-namespace) — consumers that don't install it must not import this subpackage.
"""
