# open_goal — the standing goal, armed as a Stop hook

**Set 2026-09-07.** Replaces the C1–C7 goal, whose C1, C2, C3, C5 and C6 are done and verified —
re-arming those would have kept asserting finished work. The CONSTRAINTS and the STOPPING rule below
are the original ones, verbatim.

`.claude/settings.local.json` reads this file on every Stop. The hook stops firing when
`open_lakehouse_diff_left.md` no longer exists, because that is what finishing means: an open spec is
deleted when its work lands. Pause it by creating `.claude/GOAL.paused`.

---

## GOAL

**The lakehouse is IDIOMATIC LANCE and its provenance survives a write. Two conditions, in order.**

**G1 — CLOSE C4.** Every live e2e failure classified, and the estate defects fixed. Drift is repaired
in the SUITE; a defect is repaired in the ESTATE; neither is left as "failing". Two items remain:

  * **The trainer's dedicated credential reaches the live Ray head.** Fixed in code (`96e6885f`) and
    the key is in `rask-infra-credentials`; the head still has to mount it and restart. Verified only
    when a governed training run's lineage lands attributed to `service-trainer`.
  * **`POST /ingest-media` stops answering 503.** The cascade HEAD must ASK the catalog where its
    bronze lives and write there, putting that location on the `medallion.media` trigger as
    `from_uri` — which is what `/bronze-arrival` already does for the tabular lane. The head cannot
    dictate its own location: the catalog resolves a registered RELATIVE path against the namespace's
    warehouse binding and refuses an absolute one, and `lance-catalog` is a reserved bucket no
    warehouse may claim, so a bound top-level namespace can never resolve into the platform root.

**G2 — DRAIN THE BACKLOG, BY BLAST RADIUS.** `open_lakehouse_diff_left.md` — 191 tracked, 158 open,
33 struck as of 2026-09-07. Order: anything provably wrong on the LIVE ESTATE first (the shape the
trainer 401 had — silent, data-losing, nothing red), then correctness, then tidiness. Every row
reaches a verdict: fixed, or struck with the measurement that refutes it. The file is DELETED when it
is empty, and not before — its header count is re-derived from its own rows, never asserted.

---

## CONSTRAINTS

**Never Docker — Dagger builds every image. Never mypy, never `# type: ignore` — narrow, cast, or
type it. Secrets from the Dapr secret store only — never env, never a fallback. Idiomatic to
lance-ns, never Iceberg; read lance_docs/ and cite what you read. No backward compatibility — best
practice, right design. Read skill references, not the index. Verify external claims against the
source. Comments carry rationale and provenance, never history.**

## VERIFICATION (was C7, unchanged)

**Every change verified the estate's way: per commit `uv run pytest` count, `uvx ty check` count with
the 78 pre-existing stated, `uv run ruff check`; anything deployable BUILT with Dagger, DEPLOYED to
k3s, and observed working. Never claim a thing works before showing it working. PUSH every commit —
28 sat unpushed once already.**

## STOPPING

**Stop ONLY for a decision you cannot make from the code, or a block outside the repo. Never because
a commit landed or you have something worth reporting.**
