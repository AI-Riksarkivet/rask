# open_backlog.md — what is outstanding, and what is actually PROVEN

**The rule this file exists to enforce: nothing is finished before it is proven and verified.**
A green unit test is not proof that a change works in the estate. A commit is not proof. A passing
gate is not proof. Only the behaviour, observed where it runs, is proof.

So every row carries a **verification level**, and the level is the honest one, not the hopeful one:

| Level | Means |
| --- | --- |
| **LIVE** | Observed working in the running estate — the failure reproduced first, the fix re-observed after. |
| **SUITE** | Unit/integration tests pass, gates green. **Not deployed. Not observed.** |
| **WRITTEN** | Code exists and reads correctly. No test proves it. |
| **BLOCKED** | Cannot proceed without something named in the row. |
| **OPEN** | Not started. |

A row does not move to LIVE because it looks right. It moves when someone drives it.

---

## 1. Deployed and verified live

| # | What | Proof |
| --- | --- | --- |
| 1.1 | The stage watch tells a LOST job from a slow one (`seen`, `MAX_UNSEEN_POLLS`) | **LIVE.** Failure reproduced by restarting the Ray head mid-job — new head answered `jobs: 0`, watch polled 404 indefinitely. After deploy, the two watches stuck since 06:38 and 06:48 both terminated at 06:55:3x, and fleet-wide 404 polls went to 0. |
| 1.2 | The cascade proves it may WRITE before it submits | **LIVE.** 200 + audit record observed; a refusal stops the run before the job exists. |
| 1.3 | The Ray plane runs on a scoped S3 credential; the tenant root key is off the pod | **LIVE.** Cascade succeeds; the control plane is `AccessDenied` from that credential. |
| 1.4 | No credential rides the Ray submission body | **LIVE.** The Jobs API echoes `runtime_env` to any reader; the key is gone from both submission paths. |

---

## 2. Committed but NOT verified in the estate — the honest debt

Every row here is committed and suite-green. **None has been observed running.** This is the section
that must shrink before any of it is called done.

| # | What | Level | What "proven" would require |
| --- | --- | --- | --- |
| 2.1 | **Bounded stage resubmit** on `job_vanished` / `never_registered` (owner ruling: auto-resubmit, no Redis) | **SUITE** | Deploy, restart the head mid-job **after a poll has succeeded**, and observe a resubmit followed by a completed cascade. The earlier probe accidentally landed in the pre-first-poll window and did NOT exercise this path — that miss is exactly why this row is not LIVE. |
| 2.2 | `VS-07` — a dropped search leg logs or raises; a fusion with every leg rejected no longer answers an empty 200 | **SUITE** | Drive a real search with a leg forced to fail and observe the log/5xx rather than a silent 200. |
| 2.3 | `ANN-03` — the project listing reads its actors concurrently | **SUITE** | Observe the listing latency and the peak concurrent actor reads against a real actor plane. |
| 2.4 | `VS-05` — the plain-binary listing stops reading every blob | **SUITE** | Measure bytes read serving `/api/pages` on a real plain-binary corpus. |
| 2.5 | `DUP-04` — `boto3` confined to `packages/storage`; region + session token added; timeouts everywhere | **SUITE** | Exercise a vended temporary credential end-to-end (session token is the half most likely to be silently wrong) and confirm a non-default region survives. |
| 2.6 | `ingest-flow-03` — the exact-cover search stops rescanning the universe per node | **SUITE** | Reproduce the worker's one-fragment-per-redelivered-unit recovery shape against the real staging path. |
| 2.7 | **Lakehouse `PagePreview` treats unknown payload as "attempt it"** | **SUITE** | **Browser-verified with a screenshot** — the estate's standing rule for UI changes, and this row breaks it. `svelte-check` clean is not the same as a thumbnail rendering. |

---

## 3. In flight

| # | What | Level |
| --- | --- | --- |
| 3.1 | Wire the catalog's `LANCE_LINEAGE_OUTBOX_URI` (verified absent from the render: 4× `MEDALLION_*`, zero `LANCE_*`) | in flight |
| 3.2 | Write the control-lane relay — `table_published` is the only silver→gold trigger and nothing stages or replays it | in flight |
| 3.3 | `/bronze-arrival` carries `from_uri` resolved through the catalog | in flight |
| 3.4 | Gate parity — a `key_column` naming a nonexistent column must be refused, not silently dropped | in flight |
| 3.5 | Compute-engine coupling audit + `open_compute-decoupling.md` | in flight |

---

## 4. Blocked on the owner

| # | What | Blocked by |
| --- | --- | --- |
| 4.1 | **ExternalSecrets** — operator installed, OpenBao up, manifests correct and server-dry-run accepted | The Kubernetes auth backend does not exist in OpenBao (only `token/`), and writing auth policy into the secret store is blocked by the permission classifier. The exact command has been handed over; run it with `!`. Then: seed the 3 missing KV keys (`ray-compute-access-key`, `ray-compute-secret-key`, `ray-auth-token`), flip `externalSecrets.enabled`, verify the synced Secret matches what the infra tier consumes. |
| 4.2 | **`dedicatedServiceCredentials: false`** — `LANCE_FGA_CASCADE_WRITERS` seeds `owner` on every warehouse for each mover, while `LANCE_PRIVILEGED_SUBJECTS` is deliberately unrendered | An owner ruling. The chart states the consequence in its own words: any holder of the shared `APP_API_TOKEN` could claim any name on `LANCE_SERVICE_SUBJECTS`, and those names hold `owner` on every warehouse. This is a posture, not a defect, so it is not mine to change. |

---

## 5. Known and not yet scheduled

Named so they do not read as done.

| # | What | Why it matters |
| --- | --- | --- |
| 5.1 | **No Dapr Workflow versioning seam** | `grep is_patched\|get_version\|reuse_id_policy` across `services/` and `packages/` returns nothing. Two replay divergences have already shipped. "Drain before deploying" is the only safe answer today, and it is a manual one. This is the single thing Temporal would buy that Dapr does not. |
| 5.2 | **No cascade reconciler and no re-run verb** | Zero `bindings.cron` components on the medallion; the operator surface is list/inspect/terminate with no re-drive. The cascade is forward-only AND unrepairable. |
| 5.3 | **No list-instances, therefore no sweep** | 1,367 orphan rows measured in `daprstate` with no TTL and no alert. Mitigated by retention policy, not solved. |
| 5.4 | **The workflow status metric is structurally false** | Every dying run reports success, forcing a hand-rolled parallel counter. |
| 5.5 | **Ray GCS has no fault tolerance** | Accepted deliberately (no Redis, owner ruling). A head restart still kills in-flight jobs; the platform now degrades in ~2 min with `reason=job_vanished` instead of hanging 24 h, and resubmits once deployed (2.1). |
| 5.6 | Annotator project listing has no `limit`/`cursor` | The fan-out bounds CONCURRENCY, not COUNT. A thousand projects is still a thousand actor round-trips. Needs a wire-contract change and a frontend change. |
| 5.7 | `attach_captions` logs rather than raising on an estate fault | A deliberate call — the hits are complete when it runs — but if a caption-scan outage should be a hard 503, it is one line at the same site. |
| 5.8 | No tree-wide gate forbidding `boto3` outside `packages/storage` | Per-module AST guards exist. A tree-wide gate needs an allowlist for legitimate test-side use (moto fixtures, runner tests), which is a larger decision. |
| 5.9 | Multi-base dataset garbage is unreclaimable | Upstream-blocked in Lance, not a backlog item this estate can close. Recorded so it is not rediscovered. |

---

## 6. Not in scope here

`TODO.md` holds the frontend/IA backlog (zone routes, Projects views, Explorer and annotator work).
It is a separate list with a separate owner conversation, deliberately excluded from this file so the
platform backlog does not absorb it.
