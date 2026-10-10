# 0055. A privileged credential has THREE halves (2026-09-07)

Adding `service-maintenance` to the catalog's privileged subjects, the pair everyone talks about was
in place — the service reads its own token (`catalog_compaction.dedicated_token_for`) and the door
demands it (`LANCE_PRIVILEGED_SUBJECTS`) — and it would still have 401'd every call.

**The third half is that the token must actually be SEEDED.** `openbao.yaml` derives which
`service-token-<identity>` values to mint from its OWN list; `services.yaml` derives which identities
to demand from a DIFFERENT list. Nothing made the two agree, and the disagreement is silent in the
worst direction: an unseeded identity resolves to `None`, which is the CORRECT answer for "not
provisioned", so the caller falls back to the shared bearer — and the door refuses it precisely
because the name is privileged. Both visible halves look present; every call fails.

**Caught by rendering the seed and grepping for the token before deploying**, not by a test and not
by the rollout. `test_a_privileged_subject_can_present_its_own_credential.py` now asserts all three:
the client half is discovered from the service sources, the server half from the rendered
`*_PRIVILEGED_SUBJECTS`, and the seed from the rendered OpenBao Job.

**The ordering rule, which is asymmetric and was learned the expensive way.** For a subject being
ADDED to the privileged set there is no safe gap: the client half alone is inert, the server half
alone is an outage, so they land together. For a subject ALREADY privileged the client half goes
first, because the door already expects the dedicated token. Applying the second rule to the first
case opened a live 401 window on 2026-09-07.
