"""Event-driven medallion pipeline — the ``medallion-producer`` head + the stage runners.

The medallion lakehouse pattern (bronze → silver → gold; R23 — raw is the external world, not a tier) as
**event-driven microservices** on Dapr pub/sub. ``medallion-producer`` is the **head of the pipeline**: a client
writes bronze through the catalog's namespace doors (ADR 0154), the catalog announces the write, and the
producer's ``/bronze-arrival`` subscription publishes the first trigger (``medallion.bronze``). Each stage runner
subscribes to its upstream stage's trigger and emits a standard OpenLineage transform event (so the lineage graph
grows the ``DERIVED_FROM`` edge); the catalog's publication wakes the next tier — so one bronze write cascades the
whole chain, and Dapr propagates the
W3C trace context across every hop (one distributed trace, bronze → gold).

The **fake-Ray compute** (``services/compute.py``, gated ``MEDALLION_COMPUTE_ENABLED``, default off) gives
each stage a REAL in-process Lance write, so the loop produces actual versioned data, not just provenance —
the same read→transform→write→version contract the distributed ``lance-ray`` (rask KubeRay) swaps into.
Default off → the cascade is a pure event/lineage demo (no data).

See ``docs/MEDALLION.md``.
"""
