# 0001. P0.1 — why e2e_stack.sh exists (live-verify honesty)

**Decision.** CI boots a real kind stack and runs the e2e suites (outbox, warehouses, multibase,
client-direct, CAS, governance) via `scripts/e2e_stack.sh`; a condition that cannot be proven by a grep,
a CI test, or a live assertion with a durable artifact is **not a condition, it is a claim**. The runner
additionally **fails if any test SKIPS**.

**Rationale.** Every "live-verified" claim used to rest on manual terminal runs while CI ran
`pytest -m "not e2e"`. The e2e-stack job existed but had never once gone green — a silent
`--set web.enabled=false` on a key that did not exist wedged it in `ImagePullBackOff` on every run. A CI
job that has never been green is a decoration, not a proof; and a green tick over a suite that never ran
(two suites skipped themselves on env-var name mismatches) actively buys false confidence.
