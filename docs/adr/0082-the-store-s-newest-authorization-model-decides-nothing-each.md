# 0082. The store's newest authorization model decides nothing; each component uses the model its image carries (LH-201, 2026-09-28)

Every service resolved the `lance-catalog` store at boot and took its NEWEST model, and the newest is
whoever wrote last: the catalog's boot (`fga.provision`), the chart's `openfga-model` hook, and older images
that still provision their own body (compute, controlplane and flows run images built 2026-09-03, measured
2026-09-28). No deployment pins a model, and the store held 1,353 versions.

The row's first answer was to treat a body held below the newest as a downgrade and refuse to write it.
The review reproduced why position in the history cannot carry that meaning: after a legacy writer restarts,
its narrower body is the newest and the current body sits below it, so the rule froze the store on the
legacy model, and `bootstrap-admin`, which checked and wrote tuples with no model id, then failed every
upgrade on `type 'estate' not found`.

So the newest is authoritative for nothing. Every service (`fga.resolve`), the catalog (`fga.provision`),
the hook and `bootstrap-admin` find the model whose canonical body equals their image's `model.json` in the
store's history and use its id; the hook and the catalog write a body only when the store has never held
it, before the new pods start (the hook is `post-install,pre-upgrade`). A downgrade therefore changes the
rules of the downgraded image's own pods and nobody else's. The two verifiers (`scripts/fga-store-check.sh`
and the e2e model check) ask whether the store holds the checkout's body, not whether it is the newest.
A model pin (`RASK_FGA_MODEL_ID`) contradicts this and is unused; its removal is parked as LH-309.

The same rule retires the boot-time narrowing guard (2026-09-11). It refused a body that removed a relation the
store's newest defines and answered with the newest's id instead, so the catalog checked against rules its image
did not carry, while the hook wrote the same body unguarded. Its case, an older image rolling the estate back, no
longer exists: a held body is never written again, and a body the store has never held governs only the pods built
with it. So both writers write any absent body, removals included.
