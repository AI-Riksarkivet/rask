"""Lance table maintenance service: it decides, and a bring-your-own job rewrites (ADR 0155, ADR 0162).

Its work is tasks on a NATS JetStream work queue, which a worker process of its own pulls (ADR 0198): a table is passed
when a commit or its own time asks. A table's pass expires its superseded versions (``cleanup_old_versions``, behind
the refusal ladder that guards reclamation) and records which tables need
compacting or reindexing as due records on the control root, which the catalog lists on
``GET /management/v1/maintenance/due``. The reconcile pass reports cross-store drift and, gated on a clean report,
purges expired trash. Per-table/namespace/project maintenance policies (the catalog's ``_policies/`` registry) set
retention, the cadence, and whether compaction or an index optimize may be due. Maintenance compacts nothing and builds
or optimizes no index. Run: ``uvicorn maintenance.service:app`` (the write-event lane) and ``python -m maintenance.worker``.
"""
