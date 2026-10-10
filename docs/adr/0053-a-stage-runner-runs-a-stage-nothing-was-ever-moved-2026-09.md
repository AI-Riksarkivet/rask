# 0053. A stage runner runs a stage; nothing was ever moved (2026-09-07)

**Owner ruling.** The three cascade Deployments are STAGE RUNNERS, not "movers". The old word is
inherited from the R23 wave, whose vocabulary was "tier movement" — `ingest-and-tier-movement.md`
still carries it in its filename.

**Nothing moves, and that was measured before the rename.** There is no delete, no relocation and no
rename of an upstream anywhere in the medallion. A stage runner reads a VERSION RANGE of the upstream
Lance dataset — which stays exactly where it is — runs a transform, writes a NEW downstream dataset,
emits `DERIVED_FROM`, and publishes the next trigger. Bronze is still there, unchanged, after silver
exists. That is derivation; `DERIVED_FROM` is the edge the code already emits, and `services/derivers.py`
already uses the word.

**Why the name was load-bearing rather than cosmetic.** "Mover" is why "does the workflow touch the
lakehouse?" is a natural question: movers sound like they relocate governed data. They do not — they
add to it, leaving the upstream readable at the version it was read at. A reader who believes the
name reasons about the cascade's blast radius wrongly.

**What made the rename safe is that no WIRE identity carried the word.** Measured first: Deployment
names 0 (they are `bronze-to-silver`, `silver-to-gold`, `media-to-silver`), Dapr app-ids 0, topics 0,
queue groups 0 — those key on app-id — and the frontend has no caller of the operator route. The
whole wire surface was three container commands, two env NAMES whose values are byte-identical, and
one container name.

**Two breaks were caught by diffing the RENDERED MANIFESTS against HEAD, not by a test**, and both
would have shipped green: a values key renamed to `stageRunners` while the template read
`stage_runners` rendered the chart cleanly and produced ZERO stage-runner Deployments; and `name:
mover` is a Kubernetes CONTAINER name, so `stage runner` with a space renders happily and fails on
apply. **A render that succeeds is not evidence that a rename is safe — diff the output.**
