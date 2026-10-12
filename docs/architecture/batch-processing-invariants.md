# Batch processing — the settled invariants

What the lakehouse's background work carries, and where each property is pinned. The lakehouse runs no
stage, no training and no engine ([ADR 0155](../adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)):
a batch pipeline is the user's, on their own compute, and commits through the catalog. What remains here is
the work the lakehouse does run itself — maintenance and the bus subscribers — plus the rules a
bring-your-own engine inherits. A property with no test is not an invariant, it is an intention, and the
table says which is which.

**What is deliberately NOT here.** This file records INVARIANTS and what they settled. `docs/` asserts
settled, so unfinished work placed here would read as decided regardless of the prose around it.

---

## Landed, and what pins each

| | Invariant | Where it is pinned |
| --- | --- | --- |
| **B1** | History and events carry identifiers and counts — never payloads | No test and no gate: the lakehouse hosts no batch workflow (ADR 0154, ADR 0155), so the rule binds the bring-your-own engine that runs one |
| **B2** | Never pre-gate a Ray submission on capacity or resource labels | No test: the lakehouse submits nothing, so it is a rule for the pipeline owner's engine, and no linter rule expresses it |
| **B6** | Refuse new work while draining | `service_kit.draining.retry_when_draining`, on every sidecar-delivered work door (notifications' subscriptions, maintenance's arrival, work and index routes): a draining process answers RETRY so the broker redelivers to a live replica. Pinned by `packages/service-kit/tests/test_draining_refuses_admission.py` |
| **B12** | Randomise iteration order | `services/maintenance/tests/test_the_planner_does_not_hand_out_datasets_in_listing_order.py` (the sweep's dataset list) and `tests/unit/test_maintenance_lineage.py::test_emit_sweep_lineage_caps_fail_emits_and_logs_the_drop` (its capped failure list) |

**B12 is the interesting class**: it WORKED, and nothing would have noticed losing it. Deleting
`random.shuffle(uris)` passed every test, which would silently restore the starvation the shuffle was
written for: a dataset that fails every pass sits in front of the same successors in listing order
forever. A guard that guards nothing is this estate's signature defect, and closing it is worth more
than the features filed beside it — a silently-lost invariant costs the incident it was written to
prevent, twice.

**Why B6 answers RETRY and not DROP.** On a subscription that declares a `deadLetterTopic`, daprd routes
a DROP to that topic rather than discarding it, so dropping a delivery because one replica happened to
be draining would park a good message for a human to find. RETRY hands it back to the broker
(`service_kit/draining.py` carries the daprd source references).
