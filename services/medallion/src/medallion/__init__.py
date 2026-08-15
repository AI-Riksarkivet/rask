"""Event-driven medallion pipeline — a ``medallion-producer`` producer + 3 stage movers.

The medallion lakehouse pattern (bronze → silver → gold; R23 — raw is the external world, not a tier) as
**event-driven microservices** on Dapr pub/sub. ``medallion-producer`` is the **head of the pipeline**: it ingests
external raw straight into the ``bronze$events`` / ``bronze$pages`` datasets and its ``/bronze-arrival``
subscription publishes the first trigger (``medallion.bronze``). Each mover subscribes to its upstream stage's trigger,
emits a standard OpenLineage transform event (so the lineage graph grows the ``DERIVED_FROM`` edge), and
publishes the next stage's trigger — so one source event cascades the whole chain, and Dapr propagates the
W3C trace context across every hop (one distributed trace, bronze → gold).

The **fake-Ray compute** (``services/compute.py``, gated ``MEDALLION_COMPUTE_ENABLED``, default off) gives
each stage a REAL in-process Lance write, so the loop produces actual versioned data, not just provenance —
the same read→transform→write→version contract the distributed ``lance-ray`` (rask KubeRay) swaps into.
Default off → the cascade is a pure event/lineage demo (no data).

See ``docs/event-driven-pipeline.html`` and ``docs/MEDALLION.md``.
"""
