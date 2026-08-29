# Open: estate verification after the audit-drain campaign

Working plan. Delete when every row is CLOSED. Status is counted from this file, never asserted
elsewhere — the campaign that produced it was faulted for exactly that.

**Context.** A 300-finding Python-estate audit was drained over 2026-08-28/29 (~49 commits). An
adversarial re-audit of that drain found the code broadly sound and the ledger untrustworthy; the
ledger was reconciled (`open_python-audit.md`, now 118 OPEN / 119 FIXED, counted from rows). A
capability audit then verified the platform's core use cases and found the defects below. The estate
was redeployed from main (rev 86, 2026-08-29) after 101 commits of drift — before that, nothing
observed in-cluster described current code.

## Rows

| # | Row | Status | Note |
| --- | --- | --- | --- |
| 1 | `describe_table` bound spec BODY fields as QUERY params — lance-ray/lance-ns got no credentials | **CLOSED** | `4c64046c`. Verified over a real uvicorn socket, 10 request shapes (TestClient hides this class) |
| 2 | Ingest per-run S3 `endpoint` advertised and ignored — a same-named local bucket could serve the WRONG BYTES silently | **CLOSED** | `4c64046c`. Endpoint crosses the queue; creds from the registered store's Dapr secret; unhonourable source FAILS rather than reading the wrong bucket |
| 3 | Search FGA gate authorized `declared.search` while the handler read `?table=` | **CLOSED** | `4c64046c`. Latent (no descriptor declares two), but a grant on A authorized B. Tests assert the exact FGA object checked |
| 4 | `search/similar` 400'd on every integer-keyed corpus | **CLOSED** | `4c64046c`. Reuses `keys.py`'s cast; the duplication WAS the defect |
| 5 | Seven pre-flight DROP paths halted the cascade with no lineage — so notifications told nobody | **CLOSED** | `4c64046c` |
| 6 | Compaction refused EVERY external-base dataset, incl. the cascade's own tiers — fragments accumulate forever | **CLOSED** | `531864e2`. Three independent signals, fails closed; 6 wrong-permit constructions all refused; mutation-tested; payloads re-read in a cold subprocess |
| 7 | Producer could not reach the catalog on ANY shipped chart (`MEDALLION_CATALOG_URL` gated on `qualityReview`, default false) | **CLOSED** | `531864e2`. Hoisted unconditional + render assertion at both flag states. This estate runs `qualityReview: true`, so it was invisible HERE and total everywhere else |
| 8 | Maintenance whole-estate pre-pass ignored the operator's Lance cache cap | **CLOSED** | `4c64046c`. Source-level pin — at default config the sessions coincide, so no runtime assertion can see it |
| 9 | **`/ingest-media` writes bronze with NO catalog call** — identical defect to row 7, same service | **CLOSED** | Uncommitted. `media_produce.py` registers `bronze-media$objects` through the same `catalog_register` seam, before the first blob, fail-closed → 503. RED measured: 12 failed without it (9 in `services/medallion/tests`, 3 e2e in `tests/unit`), incl. `policy/set` 404 over a real catalog socket. Chart needed NO fix — `helm template` MEASURED the catalog trio already unconditional on the producer pod (row 7 hoisted it, and `/ingest-media` is served by that same pod); the new render assertion pins the half row 7's does not — that `MEDALLION_MEDIA_BRONZE_URI` stays registerable under `MEDALLION_CATALOG_ROOT`, which `medallion.buckets` zoning would silently break |
| 10 | Rebuild + redeploy rows 6/7/9 | **CLOSED** | 14 images at `main-90b8d0ed` via Dagger, zero failures; helm rev 87 deployed, 23/23 fleet deployments on it |
| 11 | **e2e suites against current code** | **OPEN** | Only meaningful since rev 86. `test_medallion_e2e`, `test_maintenance_e2e`, `test_lineage_e2e`, `test_media_e2e`, `test_dummy_lane_e2e`, `test_annotator_catalog_live` |
| 12 | **Playwright visual pass + governance/lineage logs** | **PARTIAL** | Done against rev 86 (frontend zones are current; rows 6/7/9 are backend-only so the UI pass is valid). VERIFIED: anonymous browse redirects to Dex; catalog lists 4 pages of governed `acme-bronze$*` tables, header states the list is FGA-filtered; **lineage graph LIVE, 55 datasets**, bronze→silver→gold edges plus `compaction/*` and `lance-reconcile/*` nodes; bell 99+ so the inbox lane delivers. Repeat after row 10 |
| 13 | `catalog/services/maintenance.py:61` tells the operator the sweep refuses flag 16 "for the same reason" — after row 6 the on-demand button is the STRICTER of the two | **OPEN** | Fails safe, but the sentence is now false |
| 14 | `make openapi-check` red at HEAD — committed spec stale in 8 unrelated paths + 3 schemas | **OPEN** | Pre-existing, independent of row 1 |
| 15 | ~118 audit findings remain | **OPEN** | All triaged, none exploitable. `open_python-audit.md` |
| 16 | 9 owner decisions | **BLOCKED** | Needs the owner. Incl. ANN-05 fail-open posture |
| 17 | `lance-medallion/embed_features` + `aggregate_gold` FAILING repeatedly on the live estate | **OPEN** | Observed in the runs board: many failures 2026-08-28 → 2026-08-29T17:06, i.e. all BEFORE the rev-86 redeploy, so they describe the old code. Movers are 1/1 healthy with only health probes since restart. Needs a fresh cascade run (row 11) to know whether it recurs |
| 18 | Those failures author as `data_eng` / `analyst` — chart ROLE LITERALS, not people | **OPEN** | Per `.claude/skills/rask-notifications` trap 1, `author_subject()` reads `author.sub` only, so a failed cascade addresses an inbox actor NAMED `data_eng` and no human is told. The ORIGINATOR field is the intended fix and is what the `/train` chain already carries |

## Standing rules this work is held to

- A fix is not closed until a test FAILS without it. Several rows above were rejected the first time
  for being adjacent to the defect, or for a test that could no longer fail.
- **A fix that does not reach a shipped deployment is not a fix** (row 7 was rejected for exactly this:
  correct code behind a chart conditional that defaults off).
- Prose that a change falsifies is rewritten, not appended to. Five rejects in this campaign were
  docstrings left asserting the old behaviour.
- Counts are quoted only when personally run.
| 19 | Deletion semantics verified live | **CLOSED (mostly)** | Driven against rev 87 with throwaway fixtures under `lakehouse-wh`, then cleaned up. PROVEN: a non-empty namespace refuses **409 NamespaceNotEmpty**; a `protected` table refuses **409**; `?force=true` drops it **200**; describe then **404**; **undrop restores it 200** with a RELATIVE location (`e5e49a8d_…`, the #75 rule); describe after undrop **200** fully restored; purge + namespace drop both **200**. THE SECURITY PROPERTY HOLDS: a second identity (bob) gets **403 `can_drop required` BOTH with and without `force=true`** — force turns the protection lock only, the FGA gate runs first and identically. Post-drop describe returns 403 not 404 because the gate precedes existence resolution and the drop revoked the tuples. TWO NOTES: (a) `force` rides the QUERY STRING, not the body — a JSON `{"force":true}` is silently ignored, which cost me a false "defect" until I read `tables.py:810`; (b) the 409 reports *"contains 1 child objects"* — a COUNT, not the names, while `.claude/skills/rask-lance-catalog` says the refusal NAMES its contents. NOT yet verified: warehouse/project delete, cascade detach + plural undrop, and bucket-purge sole-ownership |
