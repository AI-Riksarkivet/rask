"""Lineage microservice — OpenLineage events → Apache AGE graph → query API.

A sibling service to the catalog (``app``): it owns the AGE lineage graph, ingests
OpenLineage run events emitted by compute/ETL jobs, and answers provenance queries
(upstream / downstream / producers). See ``docs/LINEAGE.md``.
"""
