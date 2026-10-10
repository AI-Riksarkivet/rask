# 0020. FEATURE-GAP minor deviations #1–#7 — the spec-deviation register

**Decision.** The catalog's conscious deviations from `ns_catalog/spec.yaml`, recorded so each is a
decision rather than drift (originally the retired `FEATURE-GAP.md` §1 table; #1/#3/#5/#7 since fixed):

| # | Deviation | Spec says | Status |
|---|-----------|-----------|--------|
| 1 | ~~Path/body `id` mismatch silently overrides~~ | 400 when both present **and differ** | ✅ fixed (#43) — every body-carrying `{id}` route reconciles via `core/identifiers.reconcile_body_id`; a differing body id is a 400 (the path id is what the authz gate checked, so silently picking either is wrong) |
| 2 | ~~Unsupported → HTTP **501**~~ | `UnsupportedOperationErrorResponse` is **406** | ✅ **REVERSED 2026-09-02 (Q3) and shipped**: the spec is explicit — 406 on every op that lists it, and Lance's own reference server maps `ErrorCode::Unsupported` to `NOT_ACCEPTABLE` (`rust/lance-namespace-impls/src/rest_adapter.rs:347`). "501 is arguably cleaner" lost to a spec-verbatim server. `ns_errors.py` answers 406; the prose that still said 501 in `fga_deps`, `access.py`, the catalog skill and the grants panel was corrected 2026-09-09, the panel being a live consumer that had stopped recognising an auth-off stack |
| 3 | ~~`exists` → 204~~ | 200 no-content | ✅ fixed (spec 0.9) — both `exists` endpoints return 200 |
| 4 | CreateTable ignores `x-lance-table-location` + `storage_options` | caller-chosen location/options | conscious: the catalog vends storage access (fine for single-root; a completeness gap) |
| 5 | ~~MergeInsert param set~~ | full param set | ✅ conformant since the pylance-8/spec-0.9 upgrade; residue: the FastAPI signature keeps `on` optional so the backend's own 400 answers a missing `on` (tightening would trade a spec-true 400 for a 422 — consciously left) |
| 6 | List ops omit per-request `delimiter` (`include_declared` shipped) | those params | **consciously skipped** — delimiter is deploy-fixed via `LANCE_NS_DELIMITER`; honoring it per-request would have to thread through the router-level FGA gate too (endpoint-only support would let the gate authorize a differently-parsed object — an authz-drift hazard); the native backend also cannot honor the `ListAllTables` response-joining half |
| 7 | ~~`insert` emits versionless lineage~~ | insert bumps a Lance version | ✅ fixed (GOAL 3) — `insert` reopens the dataset and stamps the real version on the WROTE edge |

**Rationale.** Each open row (#2, #4, #6) trades spec-letter conformance for a safety or architecture
property (clean 406 semantics, catalog-vended storage, authz-gate/parse coherence); recording them keeps
a future "cleanup" from reintroducing the hazard the deviation avoids.
