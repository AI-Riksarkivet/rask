"""Lance table-maintenance microservice, triggered by a Dapr cron binding.

Lance datasets degrade over time: appends/inserts accumulate small fragments, writes leave old manifest
versions, and new rows aren't covered by existing secondary indices. This service runs on a schedule — a
Dapr ``bindings.cron`` component POSTs to its route every interval — and for each Lance dataset in the
lakehouse bucket runs the three idempotent maintenance ops:

* ``optimize.compact_files()``   — merge small fragments (scan/query performance)
* ``optimize.optimize_indices()`` — keep vector/scalar/FTS indices covering new fragments (else queries
  miss recent rows or fall back to a flat scan)
* ``cleanup_old_versions()``     — GC superseded manifests

All idempotent, so a missed/retried tick is safe. Per-table/namespace #50 maintenance policies (from the
catalog's ``_policies/`` registry) can disable a dataset's maintenance, re-pace it, or override its
old-version retention; policy-less datasets keep the global defaults. (At scale these run distributed via
lance-ray's ``compact_database`` + distributed index build; here single-process.) Run:
``uvicorn compaction.service:app``.
"""
