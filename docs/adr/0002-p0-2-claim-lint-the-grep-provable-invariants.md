# 0002. P0.2 — claim-lint (the grep-provable invariants)

**Decision.** The recurring bug classes are pinned as mechanical tests in `tests/unit/test_invariants.py`,
run in CI: no bare lineage publish bypasses the outbox (the #4 uniformity invariant), every chart-injected
env var is read somewhere in `services/`, every FGA relation the code writes/checks exists in the compiled
`model.json`, and every `--set` key our scripts pass is defined in `values.yaml`.

**Rationale.** Each of these was violated silently before it was grep-proven (3 of 4 publishers bypassed
the outbox; a `--set` on a non-existent key made an unconfigured stack *look* configured). The lint is the
writing-python T6 "test every similar case in the same change" rule mechanized, so the class cannot regress.
