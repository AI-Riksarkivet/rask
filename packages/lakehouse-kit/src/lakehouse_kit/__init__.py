"""The lakehouse's kernel: what the catalog, lineage and maintenance run on, and no client imports.

The data plane (object-store resolution, the record store and the registries over it, the commit records, turns and
fences, location claims, protection and trash, table maintenance policies, the lineage outbox), the lance-plane app
factory (``lance_app``), the control lane (``control_events``, ``control_emit``, ``signature_doors``), the OpenLineage
facets the lakehouse emits (``openlineage``), the bounded Dapr publish (``dapr_publish``), the catalog's store session
(``store_session``) and the catalog's own REST transports (``lancekit``).

It builds on the platform package, ``service_kit``, which never imports it: a client pins the platform and reaches the
lakehouse only through its published doors. The data plane needs the ``lakehouse-kit[data]`` extra (pyarrow, pylance,
lance-namespace); without it only the event vocabularies and the bus seams import.
"""
