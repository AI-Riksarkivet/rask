# open_goal — the standing goal, armed as a Stop hook

**KEEP UNDER 4 000 CHARACTERS** — injected verbatim on every Stop; it reached 23 362 once by hoarding
the record of work it caused. When something here is DONE, MOVE IT OUT rather than striking it in
place. Evidence lives in `open_lakehouse_diff_left.md` and `docs/DECISIONS.md`.

`.claude/settings.local.json` reads this on every Stop; the hook stops when
`open_lakehouse_diff_left.md` is gone. Pause with `.claude/GOAL.paused`.

---

## GOAL

**ZERO TRUST (owner, 2026-09-08 — "Zero trust is the goal"), on a lakehouse that is IDIOMATIC LANCE
and whose provenance survives a write.**

**SECRETS REACH A WORKLOAD BY EXACTLY THREE PATHS AND NO OTHERS** — owner, verbatim: *"Never secret
through envs. Either from ESO, secret store dapr and STS for zero trust."*

  1. **Dapr secret store (OpenBao)** — any pod with a sidecar.
  2. **ESO** — a pod with NO sidecar (Ray lane, web zones, runners) that cannot call `/v1.0/secrets/*`.
  3. **STS short-lived credentials** — the answer for STORAGE. `vending.build_session_policy` already
     scopes by BUCKET + PREFIX with a 900 s TTL; prefer it over any long-lived key.

**NEVER through env** — not process env, not a k8s Secret via `envFrom`, not a chart value, and never a
fallback chain between them. **A SCOPED STATIC KEY IS NOT A FIX**: it shrinks the blast radius of the
wrong mechanism instead of replacing it. **AND `envFrom` IS INVISIBLE TO A SURVEY OF `env:`** — read
the RUNNING POD, never the manifest (measured 2026-09-08: five services held the RustFS ROOT credential
that way, four of them with no S3 client at all).

**G2 — DRAIN THE BACKLOG, BY BLAST RADIUS.** `open_lakehouse_diff_left.md`; re-read its own header,
which re-derives its counts from its rows. Any number written here goes stale by design.

**SCOPE — OWNER 2026-09-07, LAKEHOUSE FIRST, THEN COMPUTE, NOTHING ELSE.** Verbatim: *"prio lakehouse
and ignore other zones that are not the lakehouse or compute. I.e search, flows and model training and
annotator should be ignored and focus only on lakehouse compute services, but priotize lakehouse."*
IN: catalog, lineage, medallion, maintenance, ingest, the lakehouse halves of service-kit/storage, the
`lakehouse` zone — then `services/compute`, ray-kit, the `compute` zone. OUT: search, flows, annotator,
the TRAIN lane, the `models` zone; such a row is STRUCK with this ruling rather than worked.

**A row being PRESENT is not evidence it is in scope** — the register absorbed two drained ledgers that
swept the whole estate. **And a keyword match is not a classification**: a scan flagged nine, and
READING them saved three live lakehouse rows.

**ORDER:** anything provably wrong on the LIVE ESTATE first — silent, data-losing, nothing red — then
correctness, then tidiness. Every row reaches a verdict: fixed, or struck with the measurement that
refutes it. The file is DELETED when it is empty, and not before.

---

## CONSTRAINTS

**Never Docker — Dagger builds every image. Never mypy, never `# type: ignore` — narrow, cast, or
type it. Secrets from the Dapr secret store only — never env, never a fallback. Idiomatic to
lance-ns, never Iceberg; read lance_docs/ and cite what you read. No backward compatibility — best
practice, right design. Read skill references, not the index. Verify external claims against the
source. Comments carry rationale and provenance, never history.**

## VERIFICATION

**Per commit: `uvx ty check`, `uv run ruff check`, and the TESTPATHS the change touches. The FULL
`uv run pytest` is ~16 min — owner ruling 2026-09-08: not per commit. Run it once per batch, in the
background, and never block a commit on it (two were killed by session teardown and bought nothing).
Anything deployable BUILT with Dagger, DEPLOYED to k3s, and OBSERVED working. Never claim a thing works
before showing it working. PUSH every commit — 28 sat unpushed once already.**

## STOPPING

**Stop ONLY for a decision you cannot make from the code, or a block outside the repo. Never because
a commit landed or you have something worth reporting.**
