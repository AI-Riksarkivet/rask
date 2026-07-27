"""Event-driven medallion pipeline — a ``lance-ray`` producer + 3 stage movers.

The medallion lakehouse pattern (raw → bronze → silver → gold) as **event-driven microservices** on
Dapr pub/sub. ``lance-ray`` is the **head of the pipeline**: it produces the ``raw_events`` dataset and
publishes the first trigger (``medallion.raw``). Each mover subscribes to its upstream stage's trigger,
emits a standard OpenLineage transform event (so the lineage graph grows the ``DERIVED_FROM`` edge), and
publishes the next stage's trigger — so one source event cascades the whole chain, and Dapr propagates the
W3C trace context across every hop (one distributed trace, raw → gold).

The **fake-Ray compute** (``services/compute.py``, gated ``MEDALLION_COMPUTE_ENABLED``, default off) gives
each stage a REAL in-process Lance write, so the loop produces actual versioned data, not just provenance —
the same read→transform→write→version contract the distributed ``lance-ray`` (rask KubeRay) swaps into.
Default off → the cascade is a pure event/lineage demo (no data).

See ``docs/event-driven-pipeline.html`` and ``docs/MEDALLION.md``.
"""
