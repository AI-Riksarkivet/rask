"""Shared cross-cutting modules used by every lance-ns service.

These five modules (``secrets``, ``dapr_auth``, ``fga``, ``oidc``, ``exceptions``) are FastAPI-free (or
framework-light) infrastructure imported by the catalog, lineage, medallion and compaction services alike.
Extracted from the catalog's ``core/`` so no service depends on another service's package: every service
imports the shared pieces from ``common``, and ``common`` itself depends on none of the services.

The lance-media merge folded its shared kernel in alongside: ``lancekit/`` (Lance table
readers/writers, descriptors, predicates), ``core/`` (settings, problem+json handlers,
middleware, probes), ``schemas/``, ``state.py`` and ``deps.py`` — imported by the
viewer/search/annotator services under the same rule: services import ``common``,
never a sibling service.
"""
