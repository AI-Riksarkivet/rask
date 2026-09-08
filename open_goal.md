# open_goal — the standing goal, armed as a Stop hook

**Set 2026-09-07, trimmed 2026-09-08 to fit a 4 000-character budget.** It had grown to 23 362 — six
times over — by accumulating the record of work it had already caused. A goal that is too long to read
stops being an instruction, so the finished parts moved to where evidence belongs: G1/G1b and the twelve
§F2 verdicts are now in `open_lakehouse_diff_left.md` § F2, and "a control's NAME is not evidence that
it exists" is in `docs/DECISIONS.md`. Nothing was deleted. Keep this file under 4 000 characters: when
something here is DONE, move it out rather than striking it in place.

`.claude/settings.local.json` reads this file on every Stop. The hook stops firing when
`open_lakehouse_diff_left.md` no longer exists, because that is what finishing means: an open spec is
deleted when its work lands. Pause it by creating `.claude/GOAL.paused`.

---

## GOAL

**The lakehouse is IDIOMATIC LANCE and its provenance survives a write.**

**G2 — DRAIN THE BACKLOG, BY BLAST RADIUS.** `open_lakehouse_diff_left.md`. Re-read that file's own
header, which re-derives its counts from its own rows; any number written here goes stale by design.

**SCOPE — OWNER RULING 2026-09-07, THE LAKEHOUSE FIRST, THEN COMPUTE, AND NOTHING ELSE.** Verbatim:
*"prio lakehouse and ignore other zones that are not the lakehouse or compute. I.e search, flows and
model training and annotator should be ignored and focus only on lakehouse compute services, but
priotize lakehouse."* IN: catalog, lineage, medallion's cascade, maintenance, ingest, the lakehouse
halves of service-kit and storage, the `lakehouse` zone — then `services/compute`, ray-kit and the
`compute` zone. OUT: `services/search`, `services/flows`, `services/annotator`, the TRAIN lane and the
`models` zone; a row about them is STRUCK with this ruling as its reason rather than worked.

**A row being PRESENT is not evidence it is in scope** — this register absorbed two drained ledgers that
swept the whole estate, so its contents describe what was once audited rather than what is wanted now.
**And a keyword match is not a classification**: a scan flagged nine, and READING them saved three live
lakehouse rows that matched on Prometheus rule *annotations* and on "catalog" sitting beside "flows".

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

**Every change verified the estate's way: per commit `uv run pytest` count, `uvx ty check` count,
`uv run ruff check`; anything deployable BUILT with Dagger, DEPLOYED to k3s, and observed working.
Never claim a thing works before showing it working. PUSH every commit — 28 sat unpushed once already.**

## STOPPING

**Stop ONLY for a decision you cannot make from the code, or a block outside the repo. Never because
a commit landed or you have something worth reporting.**
