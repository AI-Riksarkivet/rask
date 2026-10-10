# 0005. P2.1 — single-base cascade write

**Decision.** The medallion/Ray cascade writes `mode="overwrite"` to **one** root; Lance multi-base (#3-B)
stays REST-create-only and is deliberately **not** wired through the stage runner write path — WONTFIX, stated as
a boundary in the `compute.py` stage runner docstring, not an accidental omission.

**Rationale.** Base registration (`initial_bases`) is create-time-only while the cascade is overwrite-only,
so distributing it would need first-write-vs-overwrite base state threaded through the stage runners — and a bare
overwrite that doesn't re-send the base silently concentrates fragments in the primary root (a live proof
flaky by construction). The pipeline already distributes at the *zone* level, and no cascade stage table is
at the per-table multi-base scale. Revisit only when a real gold/training table demonstrably exceeds
single-bucket throughput or needs cross-region DR **and** the Ray distributed-write path lands.
