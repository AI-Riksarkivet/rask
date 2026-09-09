# open_goal — the standing goal, armed as a Stop hook

**KEEP UNDER 4 000 CHARACTERS** — injected verbatim on every Stop; it reached 23 362 once by hoarding
the record of work it caused. When something here is DONE, MOVE IT OUT. Evidence belongs in
`open_lakehouse_diff_left.md` and `docs/DECISIONS.md`, never here.

Read on every Stop by `.claude/settings.local.json`; stops when the register is gone. Pause with
`.claude/GOAL.paused`.

---

## GOAL

**ZERO TRUST (owner, 2026-09-08 — "Zero trust is the goal"), on a lakehouse that is IDIOMATIC LANCE
and whose provenance survives a write.**

**SECRETS REACH A WORKLOAD BY EXACTLY THREE PATHS AND NO OTHERS** — owner, verbatim: *"Never secret
through envs. Either from ESO, secret store dapr and STS for zero trust."*

  1. **Dapr secret store (OpenBao)** — any pod with a sidecar.
  2. **ESO** — a pod with NO sidecar (Ray lane, web zones, runners); it cannot call `/v1.0/secrets/*`.
  3. **STS** — the answer for STORAGE. `vending.build_session_policy` scopes by BUCKET + PREFIX at
     900 s; prefer it over any long-lived key. A credential must never ride a field an unauthenticated
     surface echoes (Ray's `runtime_env` — §Q6-1): short-lived and published is a shorter leak.

**NEVER through env** — not process env, not a k8s Secret via `envFrom`, not a chart value, never a
fallback chain. **A SCOPED STATIC KEY IS NOT A FIX**: it shrinks the blast radius of the wrong
mechanism. **READ THE RUNNING POD, AND NEVER ONE SPELLING OF A MOUNT** — `envFrom` is invisible to an
`env:` survey, a `secretKeyRef` to an `envFrom` survey, and a DEFAULT render omits whatever is off by
default (§H8).

**G2 — DRAIN `open_lakehouse_diff_left.md`, BY BLAST RADIUS.** Its header re-derives its own counts;
any number written here is stale by design.

**SCOPE — OWNER 2026-09-07, LAKEHOUSE FIRST, THEN COMPUTE, NOTHING ELSE.** Verbatim: *"prio lakehouse
and ignore other zones that are not the lakehouse or compute. I.e search, flows and model training and
annotator should be ignored ... but priotize lakehouse."* IN: catalog, lineage, medallion, maintenance,
ingest, the lakehouse halves of service-kit/storage, the `lakehouse` zone — then `services/compute`,
ray-kit, the `compute` zone. OUT: search, flows, annotator, the TRAIN lane, `models`; STRUCK, not worked.

**PRESENCE is not scope, a keyword match is not a classification, and a VERDICT is not evidence it is
still true** — 2026-09-09: of 17 rows settled, 8 were already fixed and unstruck, 2 asked for less than
they said, 1 described the wrong thing. RE-MEASURE BEFORE WORKING A ROW.

**ORDER:** anything provably wrong on the LIVE ESTATE first — silent, data-losing, nothing red — then
correctness, then tidiness. Every row ends in a verdict: fixed, or struck with the measurement that
refutes it. DELETE the file when it is empty, not before.

---

## CONSTRAINTS

**Never Docker — Dagger builds every image. Never mypy, never `# type: ignore` — narrow or cast.
Idiomatic to lance-ns, never Iceberg; read lance_docs/ and cite it. No backward compat — best practice,
right design. Read skill REFERENCES, not the index. Verify external claims against the source. Comments
carry rationale and provenance, never history.**

## VERIFICATION

**Per commit: `uvx ty check`, `uv run ruff check`, the TESTPATHS the change touches — and ALWAYS the
invariant + integration layers, where a one-service change breaks another. Full suite **8m32s** (was
15m27s, §Q17-39): once per batch, backgrounded, no longer costly enough to skip. Anything deployable
is BUILT with Dagger, DEPLOYED to k3s and OBSERVED working — never claim it works first. PUSH always.**

## STOPPING

**Stop ONLY for a decision you cannot make from the code, or a block outside the repo — never because
a commit landed or you have something to report. A row blocked on the OWNER is named in the register
and surfaced to them; it is not a reason to stop working the rest.**
