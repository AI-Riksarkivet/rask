"""The lakehouse's library: everything the catalog, lineage and maintenance run on, and nothing a client imports.

The app factory the three services boot from (``lance_app``) and what it installs: the exceptions and problem
handlers, the request-id middleware, logging, the probes and the drain, the body cap. The auth plane (``governed``).
The data plane: object-store resolution and the S3 seam (``storage``), the record store and the registries over it,
the commit records, turns and fences, location claims, protection and trash, the maintenance policies, the Lance
readers and writers at the catalog's doors (``lancekit``). The events: the control lane (``control_events``,
``control_emit``, ``signature_doors``), the outbox and the bounded Dapr publish, the OpenLineage facets the lakehouse
emits (``openlineage``), and the emission and Ed25519 signing seam a producer and the lineage door share (``lineage``).

A client reaches the lakehouse through its published doors and events (``docs/*-openapi.json``,
``docs/COMMIT-EVENTS.md``), never by importing this package. The package root exports nothing, so ``import lakehouse``
loads none of it: every name is imported from the submodule that defines it.
"""
