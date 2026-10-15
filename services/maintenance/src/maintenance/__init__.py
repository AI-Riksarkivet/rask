"""Lance table-maintenance microservice: it decides, and a bring-your-own job rewrites (ADR 0155, ADR 0162).

A Dapr ``bindings.cron`` component POSTs to its routes every interval. The sweep expires every dataset's superseded
versions (``cleanup_old_versions``, behind the refusal ladder that guards reclamation) and records which tables need
compacting or reindexing as due records on the control root, which the catalog lists on
``GET /management/v1/maintenance/due``. The reconcile pass reports cross-store drift and, gated on a clean report,
purges expired trash. Per-table/namespace/project maintenance policies (the catalog's ``_policies/`` registry) set
retention, the cadence, and whether compaction or an index optimize may be due. Maintenance compacts nothing and builds
or optimizes no index. Run: ``uvicorn maintenance.service:app``.
"""
