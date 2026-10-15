"""Lineage microservice — the OpenLineage intake, the commit feed and the table-level edge list (ADR 0158).

A sibling service to the catalog: it records the OpenLineage events the catalog and every pipeline emit as rows
of Lance tables in the catalog's provenance namespace, serves the feed forward (``GET /events``) and what was
derived from a table (``GET /tables/{name}/derived``), and forwards every event to a configured graph backend.
See ``docs/LINEAGE.md``.
"""
