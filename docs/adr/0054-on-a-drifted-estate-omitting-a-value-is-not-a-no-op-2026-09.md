# 0054. On a drifted estate, omitting a value is not a no-op (2026-09-07)

Deploying the stage-runner rename, the scoped storage identities were deliberately HELD BACK —
`--set-string rustfs.medallionAccessKey= --set-string rustfs.maintenanceAccessKey=` — so that one
upgrade carried one attributable change. The reasoning was sound and the result was two crash-looping
services.

**Why: the seed is AUTHORITATIVE over the secret store, and the Deployments were DRIFT.** The OpenBao
seed Job writes `medallion-s3-secret-key` and `maintenance-s3-secret-key` only when the matching
access key is set. Emptying those values made the seed re-run and rewrite the store WITHOUT them. The
Deployments, meanwhile, still carried `MEDALLION_DAPR_SECRET_S3_FIELD` from a hand patch — and Helm
patches only fields that CHANGED between releases, so it had no reason to remove one it never
rendered. The pods then asked for a key nothing wrote and did the right thing:

    RuntimeError: secret 'medallion-s3-secret-key' unavailable from Dapr store 'lance-secrets'/'lance'
    — failing closed (store is the sole source)

**The rule.** Against a drifted estate, "change nothing here" cannot be expressed by leaving a value
empty: emptying it is a change relative to the drift, and it can be a change to a DIFFERENT object
than the one the value names — here, to the secret store rather than to the Deployment. Split a
deploy by what the render OWNS, not by what the values MENTION; and where a value drives both a
consumer and its provider, the two move together or not at all.

**What made it survivable rather than an outage:** the services fail closed and Kubernetes keeps the
previous ReplicaSet until the new pods are Ready, so the old pods went on serving while the new ones
crash-looped. Fail-closed plus rolling update is what turned a bad upgrade into a visible one.

**A SECOND GAP, found by curling the route rather than trusting the rollout.** The rename changed the
gateway's route table (`/api/movers` -> `/api/stage-runners`), and the gateway runs its OWN image —
not the `lance-rest-catalog` family the medallion shares. Bumping only the catalog tag left the
gateway serving the old table against a producer that had already moved, so BOTH paths 404'd: the old
one because the producer no longer serves it, the new one because the gateway did not know it. Every
pod was Ready and the cascade e2e passed 5/5 throughout, because that suite talks to the services by
ClusterIP and never traverses the gateway.

**So: a rename's blast radius is every IMAGE its changed files belong to, not every service its
changed files belong to.** The two are not the same here — five services changed prose only, and one
image (`rest-catalog`) covers seven services. The check that found it was a `curl` of the actual
route; the check that would have found it earlier is mapping changed source paths to images before
deploying, which takes one command.

**The forward fix was simply to stop holding it back** — the identities are declared in
`values-local.yaml`, so the corrective upgrade seeded both secrets, rendered both Deployments on the
scoped identity, and its post-upgrade `mc` hook created both policies and rotated both RustFS users
onto the derived secrets. Release 102 `deployed`, which also cleared a `failed` revision that had
stood since 2026-09-03.
