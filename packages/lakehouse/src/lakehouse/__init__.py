"""The lakehouse's library: everything the catalog, lineage and maintenance run on, and nothing a client imports.

The app factory the three services boot from (``service_app``) and what it installs: the problem
handlers (``problems``), the request's trace (ADR 0209), logging, the probes and the drain, the body cap. The auth plane (``governed``).
The data plane: object-store resolution and the S3 seam (``storage``), the catalog's records on the control root
(``records``: the record store and the registries over it, the commit records, turns and fences, location claims,
protection and trash, the maintenance policies), and what
every plane reads and judges a Lance table by (``lancekit``: versions, filters, absence, the commit verdict, the
manifest and its feature flags, the table layout). The events: the control lane (``control_events``,
``control_emit``, ``signature_doors``), the outbox and the process's own connection to the bus (``bus``), the OpenLineage
facets the lakehouse emits (``openlineage``), and the emission and Ed25519 signing seam a producer and the lineage door
share (``lineage``).

A client reaches the lakehouse through its published doors and events (``docs/*-openapi.json``,
``docs/COMMIT-EVENTS.md``), never by importing this package. The package root exports nothing, so ``import lakehouse``
loads none of it: every name is imported from the submodule that defines it.
"""
