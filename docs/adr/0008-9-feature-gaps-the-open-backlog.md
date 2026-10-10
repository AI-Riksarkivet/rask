# 0008. §9 — feature gaps (the open backlog)

**Decision.** The net-new feature backlog beyond the shipped parity work, kept visible as future work:
per-project **schema declaration** (see below), **claim-check** payload-size guards at every publish site
(P1) and facet-bloat caps for wide tables (P2), the **pointer-aware GC** posture and broader orphan-janitor
drive, the **run-INPUTS API** (a run's input version pins are reachable only via raw Cypher today —
needed for "which feature versions trained this model"), and the **multimodal residuals** below.

**Multimodal residuals** (re-pinned from the retired multimodal tracker so the deferrals stay citable —
`discovery.py`'s tier-2 pin resolves here):

- **Tier-2 content search** — Lance FTS + FLAT exact vector scan over dataset *content* (the rask
  `index_catalog.py`/`search_api` pattern — **both retired in the R6/R20 media wave**; content search
  re-lands as catalog-governed Lance tables behind `/api/explorer/search`); today's `/search` is
  metadata-only by design. Stays behind the
  measured recall gate (decision pin 2026-07-05, firnflow/lance_docs audit): default is FTS + FLAT exact
  scan with **no** ANN/IVF_PQ index on an embedding column unless recall@10 ≥ 0.95 against
  `bypass_vector_index=True` ground truth, re-measured on our stack (external BEIR data shows IVF_PQ
  recall loss grows with corpus size — never copy thresholds), normalized for `num_unindexed_rows`, with
  the query distance type asserted to match the index's training distance type first.
- **Catalog registration of cascade outputs** — the media-lane derived tables exist in lineage and on
  storage but are not registered as catalog tables.
- **Real-encoder deriver** — the shipped embedding deriver is deterministic pixel features (a demo
  stand-in, stated in `media.py`); a model-backed encoder slots in as a `_DERIVERS` plugin.
- **Additional-modality derivers** — audio/video/pdf slot into `_DERIVERS` (stated in `derivers.py`);
  none are built.

**Rationale.** Each is a real capability gap, un-built by explicit decision under the batch+training compass
(no query engine now), logged as tracked work rather than silently dropped.
