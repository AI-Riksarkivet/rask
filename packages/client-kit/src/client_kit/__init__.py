"""Code the lakehouse's own clients share, and the lakehouse never runs.

What several clients build the same way lives here: the media app factory and its lifespan (``client_kit.media``),
the dataset registry and the descriptor it serves (``client_kit.lancekit``), the offset-pagination envelope
(``client_kit.pagination``), and the per-route rate limiter (``client_kit.rate_limit``, behind the ``ratelimit``
extra).

This kit depends on ``service-kit`` and never the other way: no lakehouse service and no lakehouse kit imports it
(``.importlinter``, ``the-lakehouse-knows-no-client``), so deleting every client changes nothing in the lakehouse.
"""
