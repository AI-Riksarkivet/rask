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

**G1b — THE TWO SEAMS STAY BYO, AND THE DEPLOYED PATH MUST USE THEM.** Tracked as Q17-1..4.
**THE DEPENDENCY-GRAPH HALF IS DONE 2026-09-07** — re-measured after the move:

    catalog / lineage / maintenance / service-kit   0 `import ray`, 0 declared ray dependency
    medallion                                       0 `ray_kit` imports, 0 declared ray dependency
    services/compute                                declares ray-kit — THE ONE ADAPTER, by role

  `ray_kit.submit` was pure HTTPX with exactly ONE production consumer (medallion), while the ray-kit
  PACKAGE declares `ray[default]` for its SDK half. So it MOVED to `medallion.services.ray_jobs_api`,
  that service's Ray adapter, and `ray-kit` is now what `services/compute` alone needs. Gated by
  `test_no_service_depends_on_a_compute_engine.py`, whose exemption for `compute` is a ROLE (a service
  may ADAPT an engine and must not DEPEND on one) and is itself gated against becoming a blanket.

  **WHY THIS FILE'S OWN EXPLANATION WAS WRONG, and it matters because it is the reason the work
  stalled.** It said "the port was built and the callers were never migrated". They could not have
  been: `RayJobExecutor` renders `WorkOrder.to_env()` into the job's runtime_env, and the job programs
  read six differently-spelled names — **overlap 0 of 6**. Anything submitted through the port would
  have started with NO inputs bound, and an empty source URI is not a crash: it is a run that scans
  nothing, writes nothing and reports success. Converged 2026-09-07 (`32ff50cb`) across FIVE authors —
  three job programs, the submitter, the sealed `runners/dummy`, plus an e2e fixture and a runner test
  found only by grep because `runners/*` is in no root testpath. Gated whole-tree.

  **WHAT REMAINS, and it is a decision rather than a refactor.** `executor_for(...)` is still called
  nowhere outside its registry, so the deployed path reaches Ray through `ray_submit`. Putting the
  PORT in front needs a durable adapter to bind to, and `RayJobExecutor` submits a `RayJob` CR that
  KubeRay must reconcile against a `RayCluster` — of which this estate has **ZERO**: the live Ray is
  `ray-lance-head`, a HAND-APPLIED plain Deployment with no ownerReferences. Chart-owned RayCluster,
  or ephemeral per-job clusters, is an owner decision about job-record durability.

  So the LAKEHOUSE has no notion of a compute engine, and that is not to regress — the ports
  (`service_kit.lakehouse.executor` for compute, `.saga` for the workflow engine) name no engine and
  are gated by `test_the_executor_port_names_no_engine.py`.

**G2 — DRAIN THE BACKLOG, BY BLAST RADIUS.** `open_lakehouse_diff_left.md` — re-read the file's own
header, which re-derives its counts from its own rows; any number written here goes stale by design. Order: anything provably wrong on the LIVE ESTATE first (the shape the
trainer 401 had — silent, data-losing, nothing red), then correctness, then tidiness. Every row
reaches a verdict: fixed, or struck with the measurement that refutes it. The file is DELETED when it
is empty, and not before — its header count is re-derived from its own rows, never asserted.

**§F2, ZERO TRUST, WAS THE TOP OF THE BLAST-RADIUS ORDER AND IS NOW FULLY EXAMINED** — twelve of
twelve with a verdict as of 2026-09-07. The estate has not REACHED zero trust; what changed is that
nothing in this section is unknown any more. The sweep scored 19 controls (HAVE 6, STRONGER 3,
PARTIAL 8, MISSING 1) and said items 1-4 "decide whether the claim is honest":

    F2-1  per-workload storage identities — DONE for the medallion plane, and as of release 102/103
          they are RELEASE INTENT rather than drift: `helm get values` carries
          rustfs.medallionAccessKey + maintenanceAccessKey, the post-upgrade hook rotated both RustFS
          users onto DERIVED secrets, and the governed-union suite passed 5/5 after the roll (Q17-17
          closed).
          **`rask-lineage` DONE 2026-09-07**, and measuring it produced the TIGHTEST policy of the four
          because its SURFACE is smallest, not because it matters less: NO `PutObject` anywhere — it
          opens datasets read-only to probe versions and the only bytes it changes are DELETES of its
          own outbox (`outbox.drop_event`; `stage_event` belongs to the producers). The READ stays
          deliberately WIDE, which is the interesting half: it reconciles datasets this chart cannot
          enumerate, so a narrowed read is a reconciler that silently stops seeing part of the estate —
          and one that cannot read reports `known=False`, publishing nothing and looking exactly like a
          healthy cascade. DECLARED BUT NOT ARMED (`lineageAccessKey` defaults empty, so an install
          keeps the root credential), the way medallion and maintenance shipped before they were named.
          **`rask-catalog` is the ONE identity left**, and it stays a design question rather than
          another copy of the pattern: the catalog vends credentials for every runtime-minted
          warehouse, so "what may the thing that grants access itself reach?" has no answer this
          policy shape supplies.
    F2-2  fail closed in CODE — DONE 2026-09-07 (`2c69c270`). `assert_authentication_configured`
          refuses to boot a governed service whose auth is off with nobody having acknowledged it;
          the refusal is on the AMBIGUITY, not on being open. Landed for the three services that
          have a human door (catalog, lineage, the medallion producer); `maintenance`,
          `notifications` and the stage runners have none and were deliberately left out.
    F2-3  kill the one shared service bearer — THREE OF FOUR, all proven ON THE WIRE rather than in
          a render. `service-web` (`bf273f07`), `service-maintenance` and `service-ingest`
          (`9405b732`) each present their own credential; ingest's was driven both ways — the
          dedicated token answers 200 at lineage and the SAME privileged name with the shared bearer
          answers 401. A privileged credential has THREE halves (present, demand, SEED) and the third
          nearly shipped a 401 twice.
          `notifications` is the fourth and CANNOT, for a reason that is not about the service:
          landed and reverted live 2026-09-07 (release 107 → 108). Its client half is correct and its
          reconciler still 401'd — `the presented credential may not claim 'notifications'` — because
          it reaches lineage through DAPR SERVICE INVOCATION and daprd stamps its own
          `dapr-api-token` on every request it delivers. A DEDICATED CREDENTIAL IS A PROPERTY OF THE
          TRANSPORT, NOT ONLY OF THE SERVICE: ingest holds one at the same door only because it calls
          lineage directly over HTTP. The remainder is a design question — move that call off service
          invocation, or accept that sidecar-invoked hops authenticate as the estate.
    F2-4  stop laundering ANONYMOUS browser reads into a service identity — DONE 2026-09-07,
          fail closed by owner ruling. The subject held 7 reader grants across TWO tenants plus a
          writer, seeded by a documented production prerequisite; all eight revoked, both seeds
          gated, and the two e2e suites that read AS it (which is why it survived) now read as a
          user. Live: anonymous 403 / signed-in 200.

Then F2-5..12, ALL of which now have verdicts (2026-09-07):

    F2-8   the dead `static` vending mode — DONE, DELETED (Q17-12). It could be selected and never
           got: `main.py` passes no `static_keys`, so it built an empty vendor that answered None for
           everything and degraded to the mode it was chosen instead of. Gated by the GENERAL form —
           a test that parses the real call site and refuses any permitted mode needing more.
    F2-9   refuse well-known defaults — DONE (Q17-13). Four guards already existed and all four keyed
           on one OPT-IN flag; `prod-credentials.yaml` now answers "is this a real deployment?" once,
           unconditionally, on a signal an operator cannot forget.
    F2-6   TLS to every store — RE-MEASURED AND SIZED 2026-09-07, still open, and the size is the
           finding. Counted off the live Deployments rather than the templates: **171 `http://` store
           URLs**, 9 `https://` (all EXTERNAL), and one `postgresql://` with no `sslmode`. Dapr mTLS
           is the estate's only transport security and every store sits outside it.
           **THE ONE DSN IS NOT THE TRACTABLE SLICE IT LOOKS LIKE**: `SHOW ssl` on the running
           `rask-age-0` answers **off**, so the server offers no TLS at all and `sslmode=require` on
           the client would be an outage — the same asymmetric ordering as a credential's two halves.
           This is a SERVER change (certificate + `ssl=on`) before it is a connection-string one.
           Measuring it also corrected `CLAUDE.md`: AGE and OpenFGA are served by the `rask-age`
           StatefulSet, NOT CloudNativePG — zero `Cluster` objects exist, `age.cnpgCluster.enabled`
           defaults false, and the CNPG OPERATOR is installed with nothing to reconcile. An enabled
           operator toggle is not evidence the resource exists.
           **AND THAT IS ALSO THE ANSWER, which is why hand-rolling TLS here would be the wrong fix.**
           The estate has no cert-manager and the chart has never generated a certificate — but the
           CHARTED path already issues them: a CNPG Cluster gets server TLS automatically. So the AGE
           half of F2-6 is the CNPG cutover the chart already anticipates, not a StatefulSet patched
           with `genSelfSignedCert`. **Its stated blockers were re-measured and TWO OF THREE NO LONGER
           HOLD** — the note was written when they did and nobody re-checked:

               K8s 1.33+ with ImageVolume     required   ->  v1.36.2 (gate default-on at 1.35)  MET
               CNPG >= 1.27                   required   ->  operator 1.29.1                    MET
               containerd >= 2.1              required   ->  containerd 2.3.2-k3s2              MET
               the AGE extension image        required   ->  BUILT 2026-09-07                   MET

           **AND THE CUTOVER'S LAST UNPROVEN LAYER IS PROVEN TOO.** `docs/CNPG-AGE.md` recorded a
           2026-07-20 investigation: layers 1 (the AGE extension + `extension_control_path`) and 2
           (the ImageVolume prerequisites) PROVEN, and layer 3 — the operator managing a real Cluster
           — failed as an explicitly-diagnosed KIND-HOST gremlin across CNPG 1.30/1.28. On THIS estate
           the operator is 1.29.1, **1/1 Running**, leader-elected, and its log carries hourly
           `pki: Periodic TLS certificates maintenance` — the very machinery this row needs, running
           and idle for want of a Cluster. The image is `age-cnpg-ext:1.7.0-18`
           (`sha256:2b9572f1…`), verified from the REGISTRY MANIFEST rather than a build log: three
           layers matching the three `COPY` lines, including **1,497,689 B of `/lib/age.so`**.
           (`dagger core container … entries` lists a `FROM scratch` image as EMPTY — it lists
           `alpine:3.20` fine, so the manifest is the check to trust. That nearly got reported as a
           build defect.)
           **A SECOND WIN THIS ROW NEVER MENTIONED**: CNPG does PHYSICAL backups + PITR, which deletes
           the "does a logical `pg_dump` round-trip the AGE graph labels?" DR hazard —
           `scripts/age_restore_drill.sh` exists to prove against exactly that. The cutover buys TLS
           and removes a restore risk.
           **WHAT STAYS AN OWNER DECISION is the cutover itself, because it is a DATA MIGRATION**: the
           lineage graph and OpenFGA's tables live in the StatefulSet's PVC, and `age-cluster.yaml`
           deliberately fails the render if both stores are on. Building the image was cheap and
           reversible; moving the data is neither.
    F2-11  lock root create — DONE 2026-09-07 (`e6f4ce37`). THE SHIPPED DEFAULT WAS THE DEFECT, not
           its value: `hasKey` finds a key whether or not anyone chose it, so `values.yaml`'s
           `lockRootCreate: false` beat any derivation and the control could only be armed by an
           operator who already knew to arm it. The key is deleted; `services.yaml` derives it from
           `rask.isRealDeployment`, ONE helper now shared with `prod-credentials.yaml` so the two
           cannot disagree about what "real" means. Rendered three ways: local loop false, real
           deployment true with nobody arming it, explicit override winning in BOTH directions.

    F2-7   validate `register_table` locations — LARGELY REFUTED (Q17-11), by driving the deployed
           door rather than reading one layer of it: an ABSOLUTE location answers 400, a relative
           TRAVERSAL answers 400, and a plain relative path resolves inside the caller's OWN
           warehouse. §F2-7 read the Python door — which genuinely has no check — and concluded there
           was none anywhere; the enforcement lives in the native lance-ns backend beneath it. What
           remains is defence-in-depth, not the cross-tenant hole recorded.

    F2-10  correlate audit records — REFUTED (Q17-14), and instructive because the row is literally
           true and practically false. True of the CALL SITES: 118 `audit()` calls and not one passes
           a request or trace id. False of the RECORDS, which is what a compliance query reads —
           `CorrelationFilter` stamps `request_id` and `trace_id` on every record and `app.py:84`
           installs it on the ROOT handler, so `lance.audit` is stamped like any other logger that
           propagates there. Counting where a field is PASSED rather than where it is STAMPED reports
           a control missing that is present — the mirror of Q17-20, where a field that WAS passed
           turned out to be read by nothing. ITS SECOND CLAUSE IS CONFIRMED, below.

    F2-12  sign and attest images — CONFIRMED (Q17-16), with a naming trap that makes the opposite
           easy to believe. `.dagger/images.go` has a helper called `provenance()`, and it emits three
           OCI LABELS — BUILD_DATE, VCS_REF, VERSION (13 dockerfiles declare the ARGs and turn them
           into real `org.opencontainers.image.*` labels; re-checked 2026-09-07, so the row is right
           as written). No SBOM, no signature, no in-toto/SLSA attestation.
           **BUT THE FIX IS NOT "ADD COSIGN", and measuring 2026-09-07 is what says so.** The estate
           has **ZERO** signature verifiers — no Kyverno, no sigstore policy-controller, no
           Gatekeeper; its five validating webhooks are CNPG, external-secrets and Kueue. **A
           signature nothing verifies is decoration**, which is the exact anti-pattern this section
           has produced six times today, and adding one would make a seventh: a control whose NAME is
           present and whose enforcement is not. Signing needs a key custodian AND an admission-time
           verifier before it is a control, and both are owner decisions.
           **THE SBOM HALF IS DIFFERENT and partly already delivered**: `make audit` runs osv-scanner
           over six lockfiles plus `.dagger/go.mod`, `make scan-config` runs trivy over `.docker/` +
           `chart/`, and `make scan-image` runs trivy over a DAGGER-BUILT image. So the estate already
           answers "what vulnerable things are in this?" — what an SBOM adds is a PORTABLE manifest a
           downstream consumer can scan without rebuilding, which is a supply-chain claim rather than
           a scanning gap.

    F2-5   Dapr access control — ITS OWN PREMISE IS REFUTED (2026-09-07), and the refutation is what
           stopped it shipping as an outage. This file said "only TWO service-invocation callers
           exist, so a defaultAction:deny needs 11 allow entries". Both callers are real and both
           are on the HTTP `/v1.0/invoke` path — the only surface that grep could see. The estate
           invokes over THREE planes:

               HTTP /v1.0/invoke   gateway, notifications
               ActorProxy          annotator, notifications      never counted
               Dapr Workflow       flows, ingest, medallion      never counted; it IS actors

           Dapr's own docs settle half and open the other half: "Service invocation access control
           does not cover cross-app workflow scheduling" — there is a separate `WorkflowAccessPolicy`
           this estate has never heard of, a SECOND unrecorded gap. Actor-to-actor is documented
           neither way, and the estate has 28 `ActorProxy` refs behind the notifications inbox and
           the annotator's projects. The PRECONDITION does hold, measured live: Dapr mTLS true,
           Sentry running, `lance-tracing` is the one shared Configuration. What it needs first is
           the actor plane characterised on a live estate, because no document answers it.

    F2-10's SECOND CLAUSE — CONFIRMED 2026-09-07, so the row is half refuted and half proven and both
           halves needed DRIVING rather than reading. Audit records do reach GreptimeDB — 477,096 rows
           in `opentelemetry_logs` — and that is the problem. Three measurements against the live
           store: a `DELETE` on the audit stream is ACCEPTED (`affectedrows: 0`, the predicate simply
           matched nothing); the table declares `ttl = '14days'`, so every audit record is destroyed a
           fortnight after the decision it records; and both queries, the DELETE included, were issued
           to `:4000/v1/sql` with NO credentials from inside the cluster. Audit also shares ONE table
           with all other telemetry, so it can carry neither its own retention nor its own access
           policy. What the row needs is a SEPARATE append-only sink, not a setting.

**§F2 IS NOW TWELVE OF TWELVE WITH A VERDICT** — five fixed, four refuted or half-refuted, three
measured-and-open with the measurement recorded. Nothing in this section is unexamined; what is left
is work or an owner decision, never an unknown.

**THE PATTERN, now with SIX members and recorded in docs/DECISIONS.md: a control's NAME is not
evidence that it exists — and neither is its CONFIGURATION, nor a COUNT of its call sites.** Three
more landed 2026-09-07: notifications' credential, where all THREE halves rendered correctly and the
transport overwrote it on the wire; F2-5's caller count, taken on one plane of three; and A10's
dependency bump, which the catalog's own 320 tests passed because they mock the layer that refuses.
**When a measurement is a COUNT, ask what surface the count could see.** The original three: A field that WAS passed and was read by nothing (Q17-20); a field NEVER
passed that is stamped on every record anyway (Q17-14); a FUNCTION NAMED for the control it does not
implement (Q17-16). Verify where a control's value LANDS — the request on the wire, the settings
field that binds it, the record, the artifact — not where its name appears. Each took one command and
each had stood in prose for weeks.

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
