# 0007. §7a — live-verification residuals

**Decision.** A bounded set of provenance-visibility residuals is tracked (not corruption, not blocking):
overwrite leaves stale column nodes on the reused dataset id; reconcile false-flags a *deliberately* dropped
table as `MISSING_ON_STORAGE` from a stale `source_uri`; column-level lineage is emitted as a facet but not
yet stored as graph nodes/edges. Also tracked: the governed-union live evidence predates the §7a hardenings
and wants a re-run (`make e2e-governed-union`, subsumed once e2e is in CI).

**Rationale.** These are known, bounded lifecycle-emit gaps recorded so they read as deliberate residuals
rather than unproven claims. Rename on the `dir` backend is a hard refusal that emits nothing — moot, not a gap. (rask does not serve it that way: `dataplane.rename_table` moves the POINTER in-process and answers 200.)
