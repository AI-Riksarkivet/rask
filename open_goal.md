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

**~~G1 — CLOSE C4.~~ DONE 2026-09-07.** `test_governed_union_e2e` went **5 failed -> 5 passed** live,
built with Dagger and deployed to k3s. Both items below are struck; the five causes and what each
turned out to be are § Q16.

  * ~~**The trainer's dedicated credential reaches the live Ray head.**~~ **DONE 2026-09-07.** Code
    (`96e6885f`), the key in `rask-infra-credentials`, and the head repointed at it and rolled. Proven
    live: `test_train_lineage_lands_attributed_under_governance` passes, and the newest train job's
    log carries ZERO `lineage emit attempt … rejected: HTTP 401` lines where every previous run
    carried four.
  * ~~**`POST /ingest-media` stops answering 503.**~~ **DONE 2026-09-07** (`a86f5407`). The head asks
    (`ensure_stage_output`), writes where told, and names that location on the `medallion.media`
    trigger as `from_uri`; the stage runner asks where its own upstream lives and takes that as both the
    upstream and the confinement root, which NARROWS what a trigger may name rather than widening it.
    Proven live: `test_media_lane_derives_under_governance` passes.

**G1b — THE TWO SEAMS STAY BYO, AND THE DEPLOYED PATH MUST USE THEM.** Tracked as Q17-1..4. Measured 2026-09-07:

    catalog / lineage / maintenance / service-kit   0 `import ray`, 0 declared ray dependency
    medallion                                       2 files, declared dependency

  So the LAKEHOUSE has no notion of a compute engine, and that is not to regress — the ports
  (`service_kit.lakehouse.executor` for compute, `.saga` for the workflow engine) name no engine and
  are gated by `test_the_executor_port_names_no_engine.py`.

  **WHY MEDALLION STILL KNOWS ABOUT RAY: the port was built and the callers were never migrated.**
  Measured 2026-09-07:

      RayJobExecutor constructed outside tests   NOWHERE — dead on the deployed path
      the only adapter anyone builds             InProcessExecutor (transform.py:760)
      the live Ray path                          ray_submit.py, a SECOND, older submission seam
      direct ray_submit callers                  9 sites / 4 modules — workflow.py x4, train.py x3,
                                                 transform.py x1, stage_runner.py x1
      `ray` imports inside rayjob_executor.py    0 — it submits a RayJob CR over HTTPX

  So the decoupling is real for the IN-PROCESS lane and fictional for the lane the estate runs. The
  last row is the point: the port adapter needs no Ray import at all, so migrating those nine call
  sites lets `services/medallion/pyproject.toml` drop `ray-kit` — and then NO service in the estate
  depends on a compute engine, and BYO stops being a claim about ports and becomes a property of the
  dependency graph. `maintenance/services/compaction_executor.py` does not use the port either.

  A port with two adapters, one of them dead, is a decoupling claim rather than a decoupled system.

**G2 — DRAIN THE BACKLOG, BY BLAST RADIUS.** `open_lakehouse_diff_left.md` — 207 tracked, 172 open,
35 struck as of 2026-09-07 (re-read the file's own header; this line goes stale by design). Order: anything provably wrong on the LIVE ESTATE first (the shape the
trainer 401 had — silent, data-losing, nothing red), then correctness, then tidiness. Every row
reaches a verdict: fixed, or struck with the measurement that refutes it. The file is DELETED when it
is empty, and not before — its header count is re-derived from its own rows, never asserted.

**THE TOP OF THE BLAST-RADIUS ORDER IS §F2, ZERO TRUST — which this estate has NOT reached.** The
sweep scores 19 controls: HAVE 6, STRONGER 3, PARTIAL 8, MISSING 1, and §F2 states outright that
items 1-4 "decide whether the claim is honest":

    F2-1  per-workload storage identities — DONE for the medallion plane, and as of release 102/103
          they are RELEASE INTENT rather than drift: `helm get values` carries
          rustfs.medallionAccessKey + maintenanceAccessKey, the post-upgrade hook rotated both RustFS
          users onto DERIVED secrets, and the governed-union suite passed 5/5 after the roll (Q17-17
          closed). `rask-catalog` is the ONE identity left, and it is a design question rather than
          another copy of the pattern: the catalog vends credentials for every runtime-minted
          warehouse, so "what may the thing that grants access itself reach?" has no answer this
          policy shape supplies. `rask-lineage` is root too and was never counted — check it.
    F2-2  fail closed in CODE — DONE 2026-09-07 (`2c69c270`). `assert_authentication_configured`
          refuses to boot a governed service whose auth is off with nobody having acknowledged it;
          the refusal is on the AMBIGUITY, not on being open. Landed for the three services that
          have a human door (catalog, lineage, the medallion producer); `maintenance`,
          `notifications` and the stage runners have none and were deliberately left out.
    F2-3  kill the one shared service bearer — THE WIDEST HOLDER IS DONE 2026-09-07 (`bf273f07`):
          seven web pods stop mounting it and `service-web` is privileged with its own credential.
          The door already refuses a privileged name presented with the shared token, so the
          "any holder can pick the highest-privileged name" warning is false for the five cascade
          subjects too. LEFT: `service-ingest`, `service-maintenance`, `notifications` — each needs
          its CLIENT half first (all three have 0 `dedicated_token` refs), because naming a subject
          privileged before it can present its own credential 401s it outright.
    F2-4  stop laundering ANONYMOUS browser reads into a service identity — DONE 2026-09-07,
          fail closed by owner ruling. The subject held 7 reader grants across TWO tenants plus a
          writer, seeded by a documented production prerequisite; all eight revoked, both seeds
          gated, and the two e2e suites that read AS it (which is why it survived) now read as a
          user. Live: anonymous 403 / signed-in 200.

Then F2-5..12, of which THREE now have verdicts (2026-09-07):

    F2-8   the dead `static` vending mode — DONE, DELETED (Q17-12). It could be selected and never
           got: `main.py` passes no `static_keys`, so it built an empty vendor that answered None for
           everything and degraded to the mode it was chosen instead of. Gated by the GENERAL form —
           a test that parses the real call site and refuses any permitted mode needing more.
    F2-9   refuse well-known defaults — DONE (Q17-13). Four guards already existed and all four keyed
           on one OPT-IN flag; `prod-credentials.yaml` now answers "is this a real deployment?" once,
           unconditionally, on a signal an operator cannot forget.
    F2-6   TLS to every store — MEASURED live, still open: every store is plaintext (RustFS S3 from
           five services plus STS, OpenFGA, the AGE DSN, OpenBao, NATS monitor, OTLP). Dapr mTLS is
           the estate's only transport security and every store sits outside it.
    F2-11  lock root create — MEASURED live, still open: the running catalog carries
           LANCE_FGA_LOCK_ROOT_CREATE=false, so any authenticated subject may mint a top-level
           namespace ON THIS ESTATE, not merely by chart default.

Remaining with no verdict yet: F2-5 (Dapr access control + NetworkPolicy — measured tractable: only
TWO service-invocation callers exist, so a defaultAction:deny needs 11 allow entries and has ONE home
in the shared `lance-tracing` Configuration), F2-7 (validate register_table locations), F2-10
(correlate audit records), F2-12 (sign and attest images).

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
