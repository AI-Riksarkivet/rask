# open_backlog_left — SUPERSEDED

**The register is now `open_backlog_left_new.md`.** This file is a stub: it keeps the FOCUS block below,
because the session's Stop hook reads it from here, and it keeps nothing else.

WHY IT MOVED. This file reached 9,378 lines, and almost all of that was HISTORY — every row had
accumulated "re-measured on DATE", "this landed", "DEPLOYED", "the row's own estimate was wrong". A
register that carries its own changelog cannot be read, and re-reading it cost more context each session
than the work it described. The new file states, per row, only the defect, what is left, and what ends it.

Every row in the new file was re-measured against HEAD on 2026-09-18 by sixteen parallel agents reading
the CODE rather than this prose — 208 open rows in, 190 out, 18 dropped as already shipped and listed at
the foot of the new file so none vanished silently.

**A row's full story is still recoverable** — this file's complete text is in git history, and
`git log -S'<ID>' -- open_backlog_left.md` finds every commit that touched a given row.

<!-- FOCUS:START -->
## FOCUS NOW

**FINISH THE LAKEHOUSE. It is the priority and nothing else competes with it.**
The lakehouse is four services: **catalog, lineage, medallion, maintenance.**

It is done when all five hold (owner, 2026-09-10):

1. **Provenance/lineage is correct** — a write's provenance survives it.
2. **The catalog is correct for lance-ns**, and correct for **auth / authz / governance**.
3. **It is NOT coupled to a workflow engine or to Ray.** Dapr Workflow and Ray are things the
   lakehouse can be driven BY, never things it depends ON.
4. **Events are correct.**
5. **It is resilient.**

**THEN phase 2 — COMPUTE:** compute, ingest, ray-kit, and maintenance's Ray half. This is where BYO
lives: bring your own workflow engine (here Dapr Workflow) and your own distributed engine (here Ray).

**THEN phase 3 — CONTROLPLANE:** controlplane, gateway, notifications.

**LOW PRIORITY — do not work these:** flows, search, viewer, annotator.
**FRONTEND:** fix opportunistically, in the same change as the service it belongs to. Never a
frontend-only campaign.

### Standing constraints

**Secrets reach a workload by exactly three paths and no others** (owner, verbatim): *"Never secret
through envs. Either from ESO, secret store dapr and STS for zero trust."* — Dapr secret store
(OpenBao) for a pod with a sidecar; ESO for a pod without one (Ray lane, web zones, runners); STS for
STORAGE (`vending.build_session_policy`, bucket+prefix, 900 s). **Never through env** — not process
env, not a k8s Secret via `envFrom`, not a chart value, never a fallback chain. A scoped static key is
not a fix. Read the running pod, and never one spelling of a mount.

**Never Docker — Dagger builds every image. Never mypy, never `# type: ignore` — narrow or cast.
Idiomatic to lance-ns, never Iceberg; read `lance_docs/` and cite it. No backward compat. Read skill
REFERENCES, not the index. Verify external claims against the source. Comments carry rationale and
provenance, never history.**

**A VERDICT IS NOT EVIDENCE IT IS STILL TRUE — re-measure before working a row.** Of 17 rows settled
on 2026-09-09, 8 were already fixed, 2 asked for less than they said, 1 described the wrong thing.

### Verification, per commit

`uvx ty check`, `uv run ruff check`, the TESTPATHS the change touches — **and always the invariant +
integration layers**, where a one-service change breaks another. Full suite ~8m30s, backgrounded, once
per batch. Anything deployable is **BUILT with Dagger, DEPLOYED to k3s and OBSERVED working** — never
claim it works first. **Push every commit.**
<!-- FOCUS:END -->
