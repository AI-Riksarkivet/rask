# open_python-audit — the Python estate: what is wrong, and the backlog that fixes it

Working plan, **2026-08-07**, against the working tree at `0ea0b7c2` (+ uncommitted edits, which were
audited as they sit on disk). Unsettled work; **delete this file when the backlog is drained**.
`docs/` is for settled architecture only.

**No code was changed by this audit.** It is a read-only pass whose deliverable is this backlog.

## How this was produced (so you can trust or re-run it)

Every skill reference was read in full **before** any code was opened — `fastapi` (13 refs),
`writing-python` (10), `python-infrastructure` (5), `openfga`, `testing-python`, plus the vendored
`rask-architecture`, `rask-services-fleet` and `rask-lance-catalog` skills. Those references, plus the
estate's own standing rulings (Dapr over raw components; no app relational DB; secrets from the Dapr
store fail-closed; `packages/storage` as the only boto3 home; the two sanctioned service layouts), were
compiled into a 17-rule calibration sheet that every auditor was held to — so that, for example, a plain
`def` route doing blocking work is **not** reported (FastAPI threadpools it), while the same work inside
`async def` is.

14 auditors then read **every `.py` file** in `services/` (12 services) and `packages/` (7 packages) —
55 769 + 22 563 lines — one auditor per coherent scope, plus a cross-service structure mapper and a
dedicated duplication hunter. Every finding was then handed to a **separate adversarial verifier** whose
brief was to refute it: open the cited lines, judge the evidence, and reject anything sanctioned or
mischaracterised. 30 agents, ~6.5 M tokens, 1 713 tool calls.

**Evidence convention.** Every finding below carries `file:line` sites that were opened twice — once by
the auditor, once by the verifier. `CONFIRMED` = both passes reproduced the claim. `ADJUSTED` = the
mechanism reproduced but the severity or framing was corrected by the verifier (the corrected form is
what appears here). Refuted findings were dropped and are not listed. Where a verifier spotted something
the auditor missed, it is filed under **Unfiled extras** at the end of each group table.

## Scorecard

**304 findings survived verification** (40 high / 169 medium / 95 low). 1 finding was refuted outright and 36 were severity-adjusted by their verifier — the adjusted form is what appears below.

| Scope | Findings | High | Med | Low | Verifier's note on tests |
| --- | ---: | ---: | ---: | ---: | --- |
| `catalog-api` | 20 | 2 | 8 | 10 | root `tests/unit` covers it well; `services/catalog/tests` never collected |
| `catalog-core` | 18 | 2 | 11 | 5 | same — the commit-idempotency suite is inert |
| `ingest-flow` | 18 | 4 | 11 | 3 | 34 test files, the best-covered service in the estate |
| `ingest-domain` | 18 | 3 | 10 | 5 | good coverage; the list/status paths are the thin spot |
| `annotator` | 20 | 3 | 10 | 7 | no service test dir; covered indirectly from `tests/unit` |
| `lineage` | 16 | 2 | 7 | 7 | 18 tests on disk, **none collected** |
| `medallion` | 16 | 3 | 7 | 6 | covered from `tests/unit` only |
| `viewer-search` | 24 | 5 | 14 | 5 | **neither service ships tests, neither is in testpaths** |
| `maintenance` | 15 | 0 | 10 | 5 | covered from `tests/unit`; sweep/reconcile paths thin |
| `flows-fleet` | 20 | 3 | 11 | 6 | flows/gateway/compute/controlplane all enrolled and tested |
| `service-kit-core` | 22 | 1 | 10 | 11 | `packages/service-kit/tests` enrolled |
| `service-kit-governed` | 17 | 1 | 13 | 3 | enrolled, but under a blanket 21-rule ruff exemption |
| `packages-small` | 27 | 0 | 16 | 11 | storage/tracker/validate/ray-kit/lineage-kit enrolled; `ray_kit.submit` untested |
| `ratch` | 19 | 4 | 10 | 5 | **7 582 lines, zero tests, not in testpaths** |
| cross-service (structure + duplication) | 34 | 7 | 21 | 6 | — |
| **total** | **304** | **40** | **169** | **95** | |

By category: error-handling 41, resilience 33, duplication 30, config 28, resources 28, typing 23, security 18, structure 18, readability 15, dead-code 15, fastapi 14, coupling 10, testing 10, observability 10, fga 7, dapr-events 4.

## What the audit did *not* find (read this before the backlog)

The estate is in better shape than the volume above suggests, and several house rules hold **perfectly**:

- **Zero** legacy `Optional[…]` / `Union[…]` across 78 000 lines — the whole estate is on `X | None`
  and builtin generics.
- **Zero** default-arg `Depends()` — every dependency is an `Annotated[T, Depends(...)]` alias.
- **Zero** `import requests`. **One** `print()` in the whole estate. `boto3` appears in service code
  in exactly two catalog modules, both already filed (`CAT-CORE-11`, `DUP-04`); everything else goes
  through `packages/storage`.
- **Three** `@dataclass` value objects (all filed: `ANN-15`, `CAT-CORE-16`, `PS-14`) and **no genuine**
  Pydantic-v1 idiom — the `.dict()` calls in `ray-kit` are forced by Ray shipping a V1 `JobDetails`,
  and the code says so at the call site.
- **No** `@app.on_event`; every service uses an `@asynccontextmanager` lifespan.
- **No** relational database in a fleet service (the P7a ruling holds; lineage's AGE store is the
  sanctioned exception), **no** Redis, **no** `BackgroundTasks` used for pageable work.
- The catalog's 163 `run_in_threadpool` calls show the blocking-IO rule is understood — E2 is a set of
  sites that were missed when a route gained an `await`, not a service-wide misunderstanding.

The problems are concentrated in four places: **authorization coverage** (E1), **the durable-work planes**
(E3), **configuration sprawl** (E5), and **the absence of a factory for the lance-service layout**, which
is what makes eight entrypoints drift from each other (E6).

## Execution order

| Wave | Epics | Why this order |
| --- | --- | --- |
| **1 — stop the bleeding** | E1, E2, E9 | Open doors and event-loop stalls are exploitable/observable today; E9 is 2 lines of `testpaths` that make the other waves' regressions visible at all. |
| **2 — make failures honest** | E3, E4 | Durable-work correctness and the error contract. Do E4's fail-open items with E3 — they are the same incidents from two directions. |
| **3 — collapse the sprawl** | E5, E6 | Settings-per-service first (it is the precondition for `make_lance_service_app`), then the shared-platform extraction. |
| **4 — shape and speed** | E7, E8, E10, E11 | God-module splits are safest once the contracts above are pinned by tests. |
| **5 — sweep** | E12 | Deletions last, so nothing is deleted that a wave-3 refactor turns out to need. |

---

## The epics

### E1 — Open doors and mishandled credentials

**P0** · 26 issues (8 high, 12 medium, 6 low)

Authorization that is missing, asymmetric, or forgeable, plus secrets that leave the secret store. Every item here is reachable from the network by someone who should not reach it.

#### The high-severity items in this epic

<details><summary><b>FLOWS-REDOS-ON-LOOP</b> — Caller-supplied regex is compiled and executed on the event loop — a `regex` node freezes the whole flows process <i>(flows-fleet, security, effort M)</i></summary>

**Sites:** `services/flows/src/flows/executor.py:247`, `services/flows/src/flows/executor.py:253`, `services/flows/src/flows/executor.py:251`, `services/flows/src/flows/executor.py:167`, `services/flows/src/flows/executor.py:327` *(+1 more)*

**Why it matters.** `/api/*` has no auth in front of it (documented: "No auth, no app middleware"), so one `POST /api/flows/runs` with a 40-character seed takes the flows pod out until the liveness probe kills it — and `/api/health` is `async def` on the same frozen loop, so the probe fails too. `alto_lines`'s `.*?`-DOTALL scan (line 117) and `compare_texts` are the same class of unbounded CPU work in the same async path, just without an attacker-chosen exponent.

**Fix.** Run every pure-CPU node arm off the loop — `await run_in_threadpool(...)` (or `asyncio.to_thread`) inside the `regex`/`alto`/`compare`/`extract` arms of `dispatch` — and bound the regex itself: cap the subject length, and either reject nested-quantifier patterns or move the match behind an `asyncio.wait_for` on a thread. A thread does not stop the GIL stall, so the length cap is the load-bearing half. `re` alternatives with linear-time guarantees (`google-re2`) are the durable fix if user regex stays a product feature.

**Verifier (CONFIRMED).** services/flows/src/flows/executor.py: _regex is a plain sync def (:238) reached from `case "regex": return _regex(node, inputs)` inside `async def dispatch` (:168 — auditor said :167, one off), compiled at :246 (auditor said :247), finditer at :253, sub at :251. Call chain verified: routes.create_run awaits executor.execute (:116) -> asyncio.gather(run_node) (:327) -> await dispatch -> sync…

</details>

<details><summary><b>ING-01</b> — The ingest write door opens completely when APP_API_TOKEN is unset — and ingest is the only governed service with no startup guard closing that path <i>(ingest-domain, security, effort S)</i></summary>

**Sites:** `services/ingest/src/ingest/auth.py:116`, `services/ingest/src/ingest/auth.py:117`, `services/ingest/src/ingest/__init__.py:32`, `services/ingest/src/ingest/__init__.py:68`

**Why it matters.** auth.py's own module docstring states the stakes: "with `local-dir` it was one unauthenticated request to read the ingest pod's own filesystem into a governed table, and with `s3-prefix` it is an unauthenticated writer into any project's bronze tier." A blanked/rotated-away/typo'd `APP_API_TOKEN` in a Dapr-enabled deployment silently restores exactly that, and nothing reports it — the pod comes up healthy, `/api/health` is green, and the first symptom is data. medallion's identically-shaped `authorize_produce` has the same `if not expected: return`, but medallion's producer refuses to boot in that state; ingest does not, so the dev-open path is not actually confined to dev here.

**Fix.** Call `assert_app_token_configured(dapr_enabled=<the same flag the other five read>)` in `create_app()` (or at the head of `_lifespan`) so the pod crash-loops rather than serving an unauthenticated write door. Keep the dev-open branch — it is a recorded decision pinned by `test_ingest_auth.py:104` — but make it unreachable once Dapr ingest is on, and add the startup-guard test.

**Verifier (CONFIRMED).** Verified verbatim. auth.py:116-118 is `expected = os.environ.get("APP_API_TOKEN"); if not expected: return` — the entire gate. `grep -rn assert_app_token_configured services/ingest` returns nothing, while lineage/main.py:55, medallion/producer.py:57, medallion/mover.py:48, catalog/main.py:61 and maintenance/service.py:113 all call it; the helper exists at…

</details>

<details><summary><b>MED-003</b> — The S3 secret key and the estate's APP_API_TOKEN are shipped into the Ray Jobs `runtime_env`, which the Jobs API echoes back to any reader <i>(medallion, security, effort M)</i></summary>

**Sites:** `services/medallion/src/medallion/services/ray_submit.py:68`, `services/medallion/src/medallion/services/ray_submit.py:159`, `services/medallion/src/medallion/services/ray_submit.py:163`

**Why it matters.** House rule 7 makes the Dapr/OpenBao secret store the SOLE source and fail-closed; that posture is undone at the last hop by copying the plaintext secret into a REST body that the Ray dashboard/Jobs API renders back to anyone who can `GET /api/jobs/<id>` (the same dashboard `services/compute` proxies at `/api/ray/*`). `APP_API_TOKEN` is the estate-wide service credential guarding every `require_dapr_token` route — a read of one job's runtime_env yields the key to the whole cascade, `/lineage-events` included.

**Fix.** Stop transporting credentials through `runtime_env`. Mount the S3 credentials and the lineage service token onto the Ray worker pods (k8s Secret / the same OpenBao-backed Dapr secret component the fleet already uses) and pass only the non-secret pointers (`FROM_URI`, `TO_URI`, `STAGE`, `MODEL`, `REGISTRY_URI`, OTEL config) in `runtime_env`. If the KubeRay merge is the intended point for this, gate the current path behind an explicit dev-only flag so a production render cannot take it.

**Verifier (CONFIRMED).** Exact quotes verified at ray_submit.py:68 (S3_SECRET via get_secret_value), :159 (LINEAGE_SERVICE_TOKEN from APP_API_TOKEN) and :163 (S3_SECRET again), all inside body['runtime_env']['env_vars'] POSTed to /api/jobs/ (:87-91, :185-188). The module's own comment at :156-158 concedes the Jobs API echoes runtime_env back and names the S3 credentials as having the same exposure. Not a sanctioned…

</details>

<details><summary><b>SK-01</b> — Write authorship comes from an unverified, client-supplied `X-User` header <i>(service-kit-core, security, effort S)</i></summary>

**Sites:** `packages/service-kit/src/service_kit/media/deps.py:29`, `services/annotator/src/annotator/annotations/save.py:58`, `services/annotator/src/annotator/annotations/tags.py:95`

**Why it matters.** Any caller who can reach the annotator can attribute an annotation write to an arbitrary principal by setting one header. In a national-archive system the author stamp is the provenance record; a forgeable one silently corrupts the audit trail and any downstream review/attribution query, and it does so invisibly (the write succeeds, FGA passes, nothing logs an anomaly). The verified subject the service already computes makes this a wiring gap, not a missing capability.

**Fix.** Make the write paths depend on the verified subject: in the annotator, replace `author: AuthorDep` with `author: CurrentSubject` on `save_annotations` and the tags route. In service-kit, either delete `get_author`/`AuthorDep` or narrow it to read-only surfaces and rename it so the trust level is in the name (e.g. `UntrustedAuthorDep`); better, move the seam into `service_kit.governed` so the shared kit exposes exactly one identity dep and the header path cannot be reached from a governed service. If the header must survive for dev, gate it on an explicit `auth disabled` setting rather than on its own absence.

**Verifier (CONFIRMED).** Verified all three sites verbatim. `packages/service-kit/src/service_kit/media/deps.py:29-37` defines `get_author(x_user: Annotated[str|None, Header(alias="X-User")] = None)` returning `(x_user or "").strip() or "anon"`, exported as `AuthorDep`. `services/annotator/src/annotator/annotations/save.py:58` and `tags.py:95` both take `author: AuthorDep`; save.py:100-108 stamps `fields["reviewer"] =…

</details>

<details><summary><b>VS-03</b> — 25 of the viewer's 32 routes serve corpus content with no authn and no FGA gate, including every media-byte route <i>(viewer-search, security, effort L)</i></summary>

**Sites:** `services/viewer/src/viewer/api/v1/endpoints/media.py:283`, `services/viewer/src/viewer/api/v1/endpoints/media.py:161`, `services/viewer/src/viewer/api/v1/endpoints/media.py:184`, `services/viewer/src/viewer/api/v1/endpoints/media.py:242`, `services/viewer/src/viewer/api/v1/endpoints/transcripts.py:41` *(+5 more)*

**Why it matters.** The service's own `security.py` docstring is explicit that the untrusted-network posture "stops being defensible the moment more than one person can reach the zone," and that a corpus LIST alone is sensitive. Yet the list is gated while the bytes, the transcripts, the entity graph and the atlas selection rows behind it are not — anyone who can reach `:8101` (or `/api/explorer/*` through the gateway) reads every corpus in full. A gate applied to the index but not the content is not a partial control; it is a control that can be bypassed by guessing one `doc_id`.

**Fix.** Decide and apply one stance per route family. At minimum, put the byte-serving routes (`/api/media`, `/api/thumbnail`, `/api/chunk-frame`, `/api/media-clip`) behind `READ_DATA` on `corpus_object(settings, dataset_id, row_table)` and the content-listing routes (transcripts, atlas chunks, graph, voice, documents) behind `READ_METADATA`, reusing the existing `CheckerDep`/`CurrentSubject` deps — applied at the `APIRouter(dependencies=[...])` level so a new route cannot be added ungated. `POST /api/graph/cypher` needs a stronger gate than the read routes, or removal from the deployed surface.

**Verifier (CONFIRMED).** Counted directly: 32 `@router.<verb>` decorators across services/viewer/src/viewer/api/v1/endpoints/; CheckerDep appears only in datasets.py (2 routes), objects.py (3) and pages.py (2) = 7 gated, 25 ungated. Spot-checked the cited ungated sites: media.py:283 `def media(...)` streams the media blob with no subject/checker; media.py:161 thumbnail, 184 chunk-frame, 242 media-clip likewise;…

</details>

<details><summary><b>X6</b> — `search` is the only explorer service with no authn/authz code path at all — the chart's estate-wide OIDC/FGA env has nothing to bind to <i>(cross-service, security, effort M)</i></summary>

**Sites:** `services/search/src/search/core/config.py:15`, `services/search/src/search/main.py:56`, `services/search/src/search/api/v1/router.py:43`, `chart/templates/explorer.yaml:140`, `services/viewer/src/viewer/api/security.py:1`

**Why it matters.** The chart comment's premise — 'a service with no such route is unaffected' — is exactly the hole for search: on an `auth.enabled` estate, viewer gates corpus metadata and bytes on `can_get_metadata`/`can_read_data` and annotator gates its task plane, while search returns actual search HITS (row content, transcript text, filter facets) from the same corpora to any caller that can reach the gateway. `viewer/api/security.py:1` documents this precise posture as the defect it was written to close ('A corpus LIST is itself sensitive: it names data someone may not know exists') — search still has the pre-fix state, and unlike a misconfiguration it cannot be closed by any values change.

**Fix.** Give search the same door its two siblings already have and share: change `SearchSettings` to inherit `GovernedAuthSettings`, add `search/api/security.py` built on `service_kit.governed.deps.make_auth_deps` (the module viewer already uses — no new mechanism), and gate the search router on `can_read_data` for the resolved target table via a router-level `dependencies=[...]` so no endpoint can be added ungated. Add a unit test asserting all three explorer apps refuse an unauthenticated request when `LANCE_OIDC_ENABLED=true`.

</details>

<details><summary><b>catalog-api-02</b> — GET /v1/stores and /v1/stores/tiers disclose the whole estate's buckets and hosts with no authorization gate, while the sibling POST calls that same… <i>(catalog-api, security, effort S)</i></summary>

**Sites:** `services/catalog/src/catalog/api/v1/endpoints/stores.py:63`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:116`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:96`, `services/catalog/src/catalog/api/fga_deps.py:408`, `services/catalog/src/catalog/api/fga_deps.py:417`

**Why it matters.** The read of an estate-wide fact is ungated while the write of the same fact is estate-admin gated — the asymmetry means any authenticated principal (any project member) enumerates every object store the estate knows, including `endpoint` hosts of attached third-party buckets. The three comparable estate-wide surfaces all gate or filter: `/v1/events` (`require_relation` can_observe_events), `/v1/projects` (same), `/v1/warehouses` (`fga.list_objects` filter). This is the systemic hazard of the `{id}`-keyed router guard: ~30 routes whose path param is not literally `id` (`{warehouse_id}`, `{project_id}`, `{model}`) or which have none at all rely entirely on a hand-written in-handler gate, and these two were written without one.

**Fix.** Gate both reads on `can_observe_events` against `settings.fga_root_object` exactly as `attach_store` does (or FGA-filter per store if a narrower tier is wanted), and add a test that an authenticated non-estate-admin gets 403 from `GET /v1/stores`. Separately, add a contract test that enumerates every mounted route and asserts each one either matches a `_RESOURCES` prefix (router-gated) or names an explicit `require_relation`/`require_can_*` call — so the next ungated estate surface fails a test rather than shipping.

**Verifier (CONFIRMED).** Verified. stores.py:63-66 `list_stores(state: UserStateStoreDep)` and :121-132 `stores_by_tier(state: UserStateStoreDep)` take no token/client and call no require_relation, while the write sibling at stores.py:96 does `require_relation(..., relation="can_observe_events", obj=settings.fga_root_object)`. The router-level guard cannot cover them: api/v1/router.py mounts every module under…

</details>

<details><summary><b>ratch-003</b> — Every `AWS_*` env var — including the secret access key — is copied into the Ray Job's runtime_env <i>(ratch, security, effort M)</i></summary>

**Sites:** `packages/ratch/src/ratch/core/jobs.py:47`, `packages/ratch/src/ratch/core/jobs.py:160`, `packages/ratch/src/ratch/core/jobs.py:164`

**Why it matters.** `AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN` match that prefix and land verbatim in the job's `runtime_env.env_vars`. Ray persists the submitted runtime_env in the job record and serves it back through the dashboard's job API (`GET /api/jobs/<submission_id>`) and the Jobs UI — and `JobsSettings.ray_address` defaults to `http://127.0.0.1:8265` (:70), the unauthenticated Ray dashboard this repo runs (CLAUDE.md: "No auth, no app middleware. The services assume localhost / trusted network"). So a long-lived credential is written into a queryable, plaintext, persisted control-plane record every time a runner is dispatched. This is precisely the env-var secret propagation HOUSE-RULE-7 forbids: the Dapr/OpenBao secret store is the sole sanctioned source, fail-closed, and the memory note is explicit — "OpenBao via Dapr is the SOLE source, fail-closed; no env fallback".

**Fix.** Stop forwarding a credential-bearing prefix wholesale. Narrow `_FORWARDED_ENV_PREFIXES` to non-secret configuration only (`MEDIA_`, plus explicitly named non-secret S3 knobs like `RASK_S3_ENDPOINT_URL`/`AWS_REGION`/`AWS_DEFAULT_REGION` via an allow-list of exact names, not a prefix), and have the runner obtain its store credentials the same way the fleet does — from the Dapr secret store at worker start, failing closed if absent. If the local dev loop genuinely needs creds on the worker, that belongs in the cluster's own secret material (a k8s Secret mounted into the Ray worker pod), never in a per-job submission body.

**Verifier (CONFIRMED).** Verbatim: jobs.py:47 `_FORWARDED_ENV_PREFIXES = ('MEDIA_', 'AWS_')`; :160 `forwarded = {k: v for k, v in os.environ.items() if k.startswith(_FORWARDED_ENV_PREFIXES)}`; :164 returns it as `env_vars`, and `_submit_or_resubmit` passes that dict as the job body's `runtime_env` (:176-180). `AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` match the prefix, and Ray persists+serves runtime_env.env_vars…

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `ANN-12` | med | S | Arrow-IPC import reads and materializes an unbounded request body | `services/annotator/src/annotator/api/v1/endpoints/tasks.py:335` |
| `CAT-CORE-02` | med | S | STS session policy is built by unescaped interpolation — a wildcard in a table name widens vended credentials to sibling tables | `services/catalog/src/catalog/core/vending.py:103` |
| `GW-502-LEAKS-INTERNAL-ADDRESS` | med | S | The 502 body prints the internal upstream address to the public caller — the exact leak `_rewrite_location` exists to prevent | `services/gateway/src/gateway/__init__.py:333` |
| `ING-05` | med | M | GET /ingests authorizes with a loop of up to 200 sequential FGA checks instead of batch_check/list_objects | `services/ingest/src/ingest/api.py:243` |
| `MED-011` | med | M | The medallion writes OpenFGA tuples directly from a message handler — a second tuple-write path outside the catalog's `seed_ownership` | `services/medallion/src/medallion/services/train.py:239` |
| `SKG-02` | med | S | Partial batch failure in write_tuples/delete_tuples skips the audit trail entirely for the tuples that DID land | `packages/service-kit/src/service_kit/governed/fga.py:1058` |
| `SKG-03` | med | M | expand_tree reports every repeated object#relation as `cycle: True`, so an ordinary concentric diamond is mislabelled a loop and its… | `packages/service-kit/src/service_kit/governed/fga.py:725` |
| `VS-09` | med | M | `/api/media-clip` hands a `Host`-header-derived URL to ffmpeg — server-side request forgery | `services/viewer/src/viewer/api/v1/endpoints/media.py:276` |
| `VS-10` | med | S | Unsanitized S3 object key interpolated into the `Content-Disposition` header | `services/viewer/src/viewer/api/v1/endpoints/objects.py:329` |
| `VS-13` | med | L | The search service has no authn/authz at all yet accepts a raw SQL `where` expression ANDed into every query | `services/search/src/search/api/v1/router.py:294` |
| `X9` | med | S | `make_service_app` publishes /docs, /redoc and /openapi.json unconditionally, while all seven lance services gate them behind a *_DOCS… | `packages/service-kit/src/service_kit/__init__.py:126` |
| `catalog-api-12` | med | S | The batch authorizer loops sequential fga.check calls for owner-tier operations instead of batch_check | `services/catalog/src/catalog/api/fga_deps.py:383` |
| `F-LIN-13` | low | S | A loop of sequential single check() calls where the module's own batch_check filter is available, and a duplicate-laden batch payload | `services/lineage/src/lineage/api/v1/endpoints/demo.py:148` |
| `ING-17` | low | S | The queue diagnostic returns raw exception text in its response body, and the catalog's existence probe treats any exception as 'absent' | `services/ingest/src/ingest/queue_health.py:125` |
| `MAINT-14` | low | S | docs_enabled defaults to True and the chart never sets it false, so /docs and /openapi.json are served in production despite the comment… | `services/maintenance/src/maintenance/core/config.py:32` |
| `MED-010` | low | S | The service-token comparison (env read + dev-open + compare_digest) has a second home in `authorize_produce`, though the dual-auth… | `services/medallion/src/medallion/api/produce_auth.py:76` |
| `PS-21` | low | S | `proxy()` returns the raw, untruncated exception string — including the internal dashboard URL — to the browser | `packages/ray-kit/src/ray_kit/dashboard.py:578` |
| `catalog-api-06` | low | M | Three tuple write/revoke call sites bypass the seed_ownership/revoke_ownership seam, each for a documented reason | `services/catalog/src/catalog/api/v1/endpoints/warehouses.py:473` |

### E2 — Blocking I/O on the event loop

**P0** · 8 issues (4 high, 3 medium, 1 low)

`async def` handlers that call synchronous pylance / boto3 / PyJWT-JWKS / Dapr code. Each one stalls its whole process — including the k8s probes — for the duration of an object-store round trip.

#### The high-severity items in this epic

<details><summary><b>ANN-01</b> — Blocking Lance/S3 dataset resolution runs on the event loop inside async routes <i>(annotator, resources, effort S)</i></summary>

**Sites:** `services/annotator/src/annotator/api/v1/endpoints/assist.py:217`, `services/annotator/src/annotator/api/v1/endpoints/project_events.py:332`, `services/annotator/src/annotator/api/v1/endpoints/project_events.py:340`, `services/annotator/src/annotator/api/v1/endpoints/project_events.py:408`

**Why it matters.** HOUSE-RULE-4: pylance/lancedb/file IO are sync; calling them inside an `async def` route blocks the event loop. Worse than a single stall — the resolution is serialised behind a `threading.Lock`, so a second coroutine hitting a cold miss blocks the *whole* loop for the duration of an S3 open, freezing every other in-flight request in the pod (probes included). The sibling `def` routes in `annotations/wire.py:39`, `save.py:56`, `tags.py:93`, `versions.py:50` call the same helper correctly on the threadpool, which shows the intended shape.

**Fix.** Either make `assist` and `send_items` plain `def` (they already push their one genuinely-async producer call through `run_in_threadpool`, so the inversion is small), or wrap the resolution: `handle = await run_in_threadpool(dataset_handle, state, dataset)` and `await run_in_threadpool(_refuse_unknown_datasets, state, payload)`. Prefer the latter for `send_items`, which must stay `async` for its actor awaits.

**Verifier (CONFIRMED).** Verified. assist.py:208 `async def assist` calls `handle = dataset_handle(state, dataset)` at :217; project_events.py:386 `async def send_items` calls `_refuse_unknown_datasets(state, payload)` at :408, whose body loops `dataset_handle(state, name)` (:332) and `state.registry.list_ids()` (:340). service_kit/media/state.py:68 `dataset_handle` -> lancekit/registry.py:99 `DatasetRegistry.get` does…

</details>

<details><summary><b>ING-02</b> — Blocking OIDC discovery + JWKS network IO runs inside async def — every bearer-authenticated ingest request can stall the event loop for up to 15 s <i>(ingest-domain, fastapi, effort S)</i></summary>

**Sites:** `services/ingest/src/ingest/auth.py:151`, `services/ingest/src/ingest/api.py:127`, `services/ingest/src/ingest/api.py:248`, `services/ingest/src/ingest/api.py:289`

**Why it matters.** One cold cache, one TTL expiry, or one IdP key rotation blocks the single event loop of the whole pod — every in-flight ingest POST, every status poll and `/api/health` queue behind a 15-second synchronous HTTP GET. The service already demonstrates it knows the rule (`_DaprWorkflowStarter.start` explicitly uses `to_thread` because "the client is SYNCHRONOUS gRPC, so calling it directly would block the event loop for every other request") and then does the forbidden thing one module over. In `list_ingests` the verifier is called once per record in a loop, so a cache miss lands inside an N-iteration path.

**Fix.** Wrap the verification in `await asyncio.to_thread(verifier.verify, raw)` (or `asyncer.asyncify`) at auth.py:151 — one line, no signature change. Longer term, give `OIDCVerifier` an async `verify` backed by the app's shared `httpx.AsyncClient` so the discovery/JWKS fetches are awaited rather than threaded.

**Verifier (CONFIRMED).** auth.py:98 is `async def authorize_ingest` and line 151 calls `verifier.verify(raw)` unwrapped; oidc.py:186 is `with httpx.Client(timeout=15.0)` inside `_resolve`, reached from verify→_provider_for on cache miss/TTL expiry, and line ~229 `provider.jwk_client.get_signing_key_from_jwt(token)` fetches JWKS synchronously. All three call sites (api.py:127, 248, 289) await it from `async def` routes,…

</details>

<details><summary><b>VS-01</b> — Object-browser routes are `async def` but run blocking boto3 and a blocking Dapr secret fetch on the event loop <i>(viewer-search, fastapi, effort S)</i></summary>

**Sites:** `services/viewer/src/viewer/api/v1/endpoints/objects.py:225`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:266`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:301`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:70`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:99` *(+3 more)*

**Why it matters.** Every S3 list/HEAD/GET and every cold secret lookup stalls the whole viewer process — all other in-flight requests, including `/livez`/`/readyz`, block behind one slow bucket listing or one unseeded secret store. The gate (`await checker(...)`) is what forced these functions to become coroutines; the body was never moved off the loop, and the docstring was never updated, so the file now documents the opposite of what it does.

**Fix.** Split the awaited authorization from the blocking body: keep `async def` for the `await _require_browse(...)` prologue and push everything from `_client_for(...)` onward through `fastapi.concurrency.run_in_threadpool` (the pattern `pages.py:_authorized_dataset` already uses), then correct the module docstring. Same treatment for `_creds`.

**Verifier (CONFIRMED).** Verified at services/viewer/src/viewer/api/v1/endpoints/objects.py. Module docstring lines 10-11 literally say "Routes are sync ``def`` — FastAPI runs the blocking boto calls in its threadpool", while all three routes are coroutines: `async def list_objects` (225), `async def head_object` (266), `async def download_object` (301). Bodies do blocking boto3 inline on the loop: paginator.paginate at…

</details>

<details><summary><b>VS-02</b> — Dataset enumeration routes are `async def` and open Lance/S3 (under a threading.Lock) inline on the event loop <i>(viewer-search, fastapi, effort S)</i></summary>

**Sites:** `services/viewer/src/viewer/api/v1/endpoints/datasets.py:64`, `services/viewer/src/viewer/api/v1/endpoints/datasets.py:76`, `services/viewer/src/viewer/api/v1/endpoints/datasets.py:78`, `services/viewer/src/viewer/api/v1/endpoints/datasets.py:101`, `services/viewer/src/viewer/api/v1/endpoints/datasets.py:123` *(+2 more)*

**Why it matters.** Worse than plain blocking IO: because `registry.get` holds a `threading.Lock`, the event loop can end up spinning on a lock owned by a threadpool worker that is mid-S3-open, so the entire process serializes behind one cold dataset. This is the first call every zone makes on page load, so the stall is on the hottest path. The sibling `voice.py:141` and `pages.py:137` already document and apply the correct `run_in_threadpool` treatment for exactly this call.

**Fix.** Wrap the registry work: `datasets = await run_in_threadpool(_collect_summaries, state)` and `handle = await run_in_threadpool(dataset_handle, state, dataset_id)`, leaving only the `await checker(...)` calls on the loop. Also hoist the per-dataset `registry.get` out of `_may_see` so the descriptor is read once, not twice.

**Verifier (CONFIRMED).** Verified. datasets.py:64 `async def list_datasets`, loop at 76 calling `registry.get(dataset_id).descriptor` at 78 inline; `_may_see` (100-106) calls `_row_table` → `registry.get` again (123) inside `asyncio.gather` (108); `dataset_descriptor` (128) calls `dataset_handle(state, dataset_id)` inline at 136. DatasetRegistry.get (packages/service-kit/src/service_kit/lancekit/registry.py:109-134)…

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `F-LIN-05` | med | S | Uncached blocking secret-store fetch with a ~2-minute retry budget runs on every privileged-service request | `services/lineage/src/lineage/api/security.py:75` |
| `MAINT-07` | med | S | reconcile() builds a boto3 S3 client per call inside an async def when the lifespan-built client is absent — blocking construction on the… | `services/maintenance/src/maintenance/services/reconcile.py:704` |
| `SK-13` | med | S | `Settings.storage_options` is a property that performs a blocking Dapr secret fetch and raises | `packages/service-kit/src/service_kit/media/config.py:167` |
| `catalog-api-18` | low | S | An async route performs uncached blocking file I/O to read the authorization model DSL | `services/catalog/src/catalog/api/v1/endpoints/access_admin.py:318` |

### E3 — Durable-work correctness: Dapr Workflow, JetStream, actors

**P1** · 31 issues (8 high, 21 medium, 2 low)

The ingest/medallion/flows/annotator planes lose, duplicate, or strand work. These are the findings where a crash, a redelivery, or a rolling restart produces a wrong result rather than a slow one.

#### The high-severity items in this epic

<details><summary><b>ANN-02</b> — Publish saga is fire-and-forget with a module-global in-flight set — a lost task strands the project in `publishing` forever <i>(annotator, resilience, effort M)</i></summary>

**Sites:** `services/annotator/src/annotator/projects/lakehouse.py:277`, `services/annotator/src/annotator/projects/lakehouse.py:280`, `services/annotator/src/annotator/projects/lakehouse.py:307`, `services/annotator/src/annotator/projects/project_actor.py:433`

**Why it matters.** HOUSE-RULE-15 (create_task without holding a reference — asyncio keeps only a weak reference, so a suspended task can be GC'd) and HOUSE-RULE-11 (module globals holding mutable state). The failure is not merely a lost run: if `_drive` is collected or cancelled without its `finally` completing, `_RUNNING` retains the project id, every subsequent 60 s watchdog tick logs "already running — the tick stands down" (`:283`), and `PROJECT_EDGES` (machines.py:38-39) has **no principal-fireable edge out of `PUBLISHING`** — `SYSTEM_ONLY_EVENTS` refuses `publish_succeeded`/`publish_failed` over HTTP (project_events.py:184). The project is unrecoverable without a pod restart, which is exactly the stranding the watchdog reminder exists to prevent.

**Fix.** Hold a strong reference (`app.state`-scoped `set[asyncio.Task]` with `task.add_done_callback(tasks.discard)`), and make `_RUNNING` an attribute of an object owned by the lifespan rather than a module global. Better: make the guard self-healing — store a monotonic start timestamp instead of a bare id and let a tick re-drive once the entry is older than one period, so a lost task costs one extra tick rather than the project.

**Verifier (CONFIRMED).** Verified line-for-line. lakehouse.py:277 `_RUNNING: set[str] = set()`; :280-285 guard + add; :304-305 `finally: _RUNNING.discard(...)`; :307 `return asyncio.get_running_loop().create_task(_drive())`. Sole caller project_actor.py:433 `lakehouse.spawn_publish(project.project_id)` discards the Task — no strong reference anywhere. The stranding chain checks out: machines.py:37-38 give PUBLISHING…

</details>

<details><summary><b>CAT-CORE-01</b> — Commit idempotency guard fails OPEN on any storage error, re-enabling the duplicate-append it exists to prevent <i>(catalog-core, error-handling, effort S)</i></summary>

**Sites:** `services/catalog/src/catalog/services/dataplane.py:621`, `services/catalog/src/catalog/services/dataplane.py:623`, `services/catalog/src/catalog/services/dataplane.py:629`, `services/catalog/src/catalog/services/dataplane.py:632`, `services/catalog/src/catalog/services/dataplane.py:675`

**Why it matters.** This is the one guard standing between a Dapr activity replay and duplicated bronze rows; `services/catalog/tests/test_commit_idempotency.py:9-10` records that the sibling hole was measured at "nine copies per file". Append never conflicts with Append, so nothing downstream refuses the duplicate — a single transient read failure during a retry silently corrupts the table's row count. The per-version `continue` has the same shape: a transient failure reading OUR OWN marker version skips it and the replay appends again.

**Fix.** Discriminate: treat only a genuinely-absent dataset (`FileNotFoundError` / lance's "was not found" message shape, the discrimination `read_blob` already performs at dataplane.py:804-815) as "no prior commit"; let every other exception propagate as `ServiceUnavailableError` so the caller retries rather than duplicating. Same for the per-version read: skip only on a missing/GC'd transaction file, propagate on a store error. Add a test that injects a raising storage layer and asserts the commit is refused, not duplicated.

**Verifier (CONFIRMED).** Verified at dataplane.py:620-637. `_find_run_commit` has exactly the two blanket `except Exception` handlers cited: line 623 returns None on ANY failure of `lance.dataset(location, ...)` with the comment "no dataset yet -> certainly no prior commit by this run", and line 632 `continue`s past ANY failure of `read_transaction(version)`. `commit_appended_fragments` (line 675-678) treats `None` as…

</details>

<details><summary><b>MED-001</b> — The publication cascade head puts the project-QUALIFIED NAMESPACE on the trigger's `project` field, so every real publication is DROPped by the mover <i>(medallion, dapr-events, effort M)</i></summary>

**Sites:** `services/medallion/src/medallion/services/publication_trigger.py:99`, `services/medallion/src/medallion/services/publication_trigger.py:122`, `services/medallion/src/medallion/services/transform.py:126`, `services/medallion/src/medallion/services/transform.py:143`, `services/medallion/src/medallion/services/transform.py:189` *(+2 more)*

**Why it matters.** This head exists specifically to fix defect B8 ("the cascade moves no data"). As wired it reproduces B8 through a different door: every publication of an ingest-written table is DROPped with a FAIL run, or — where the registry happens to resolve — emits double-qualified lineage identities (`bind86-bronze-bronze$pages`) that name a dataset nothing else in the estate uses. It is the exact drift `ingest/naming.py`'s own docstring warns about, on the one side that module did not reach.

**Fix.** Derive the project from the namespace rather than equating them: strip the tier suffix (`namespace.removesuffix(f"-{settings.bronze_namespace}")`, empty result → single-tenant) or, better, have the catalog carry the resolved `project` explicitly in `extra` the way it already carries `location`/`from_version`, and read it there. Then extend `tests/unit/test_publication_trigger.py` with the real id shape (`table:bind86-bronze$pages` → `project == "bind86"`), asserting against `ingest.naming.bronze_namespace_for` so the two sides cannot drift again.

**Verifier (CONFIRMED).** Every link in the chain verified. catalog/api/v1/endpoints/publication.py emits object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"; the ingest plane's table id IS project-qualified (ingest/naming.py:56-70 bronze_namespace_for -> project_namespace(tenant, 'bronze') -> 'bind86-bronze', bronze_table_id -> 'bind86-bronze$pages', used by ingest/lineage.py:118-119 and…

</details>

<details><summary><b>MED-002</b> — The in-process HTR lane holds the Dapr ack — and a process-wide asyncio.Lock — across the entire multi-page GPU transcription <i>(medallion, resilience, effort L)</i></summary>

**Sites:** `services/medallion/src/medallion/services/transform.py:225`, `services/medallion/src/medallion/services/transform.py:256`, `services/medallion/src/medallion/services/htr_stage.py:82`, `services/medallion/src/medallion/core/config.py:174`, `services/medallion/src/medallion/services/ray_submit.py:103`

**Why it matters.** ackWait expires long before the stage returns, so the sidecar redelivers; each redelivery enters `handle_stage`, blocks on `_write_lock`, and pins a Starlette threadpool worker (default 40) while waiting. When the lock finally releases the redelivery re-reads every page and re-runs every GPU inference from scratch — N× the compute cost per redelivery, up to `maxDeliver`, then the DLQ. The A13 rework removed exactly this shape from the Ray stage path but left it on the in-process/HTR path, which `transform.py:249-254` dispatches to FIRST, ahead of the Ray branch.

**Fix.** Give the HTR lane the same submit-and-ack shape the Ray stage path already has: dispatch the transcription as a job (or an internal durable task) and return SUCCESS immediately, letting the registered commit + publication event wake the next tier. Until P7b lands, at minimum bound the handler (`asyncio.timeout` well inside ackWait, returning RETRY) and make `_write_lock` acquisition non-blocking (`if _write_lock.locked(): return _RETRY`) so redeliveries do not queue up holding workers.

**Verifier (CONFIRMED).** Verified: transform.py:225 `async with _write_lock:` wraps the whole compute, :249-270 dispatches the HTR lane FIRST (before the ray branch), htr_stage.py:82-89 loops every bronze page with a blocking httpx call bounded by htrflow_timeout_seconds (config.py:174, default 600.0), and ray_submit.py:103-106 states the ack rule this breaks. The lane is live by default, not a toggle: chart…

</details>

<details><summary><b>ingest-flow-01</b> — Workflow control flow branches on import-time os.getenv values — a replay on a pod with different env changes the recorded task sequence <i>(ingest-flow, dapr-events, effort M)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:67`, `services/ingest/src/ingest/workflow.py:82`, `services/ingest/src/ingest/workflow.py:218`, `services/ingest/src/ingest/workflow.py:294`, `services/ingest/src/ingest/workflow.py:110` *(+3 more)*

**Why it matters.** dapr-workflows.md's determinism table lists `os.environ[...]` reads as a workflow-function prohibition: "Pass via input or activity". A workflow replays from history on whichever pod picks it up. If a rolling restart, a `kubectl set env`, or a values change moves `RASK_INGEST_MAX_RUN_HOURS` between 0 and non-zero while a run is in flight, the replay either creates a durable timer the history has no record of, or skips one the history has — a task-sequence divergence, which is the exact class of change that wedges a durable workflow permanently. `MAX_UNITS` has the milder version: the same run reports FAILED-by-ceiling or COMPLETE depending on which pod replayed it. This plane already knows the rule — `sizing` is documented as "resolved ONCE, at accept, and carried" precisely so "a rolling restart cannot change a live run's fragment size mid-fan-out" — and then leaves the two ceilings,…

**Fix.** Resolve both ceilings at ACCEPT (`api.create_ingest`, where `sizing` is already resolved) and carry them as required fields on `RunSpec`; read them from `spec` inside the workflow, never from module state. Make `sizing` a required field on `RunSpec`/`ChunkSpec` rather than `default_factory=resolve`, so no env read is reachable from a workflow-body `model_validate`; keep the compat default only behind an explicit accept-time migration. Add a test that runs `ingest_run` twice over the same recorded history with the env changed between and asserts the yielded task sequence is identical.

**Verifier (CONFIRMED).** Every cited line is exact. workflow.py:67 `MAX_RUN_HOURS = float(os.getenv("RASK_INGEST_MAX_RUN_HOURS", "0") or 0)` and :82 `MAX_UNITS = int(os.getenv("RASK_INGEST_MAX_UNITS", "0") or 0)` are import-time module state; :218 `if MAX_UNITS > 0 and units_total > MAX_UNITS:` and :294 `if MAX_RUN_HOURS > 0:` both sit inside the `ingest_run` generator body. The MAX_RUN_HOURS branch is the serious one —…

</details>

<details><summary><b>ingest-flow-02</b> — Three of four NATS connect sites have no timeout, against the file's own measured evidence that a connect to a dead broker never returns <i>(ingest-flow, resilience, effort S)</i></summary>

**Sites:** `services/ingest/src/ingest/runtime.py:192`, `services/ingest/src/ingest/runtime.py:232`, `services/ingest/src/ingest/runtime.py:291`, `services/ingest/src/ingest/queue.py:127`

**Why it matters.** These three sites are the bodies of the `publish_units`, `drain_chunk` and `reconcile_chunk` ACTIVITIES. A Dapr activity has no execution timeout, so an unreachable or slow-to-DNS broker hangs the activity indefinitely: `ACTIVITY_RETRY` never fires (nothing raised), the child workflow never returns, the parent's `when_all` never completes, and the run sits RUNNING forever with no error anywhere. The one place a bound exists is the tidy-up path, which is the least important of the four. Resilience guidance is "timeouts everywhere — every network call"; here the plane measured the failure, wrote the fix, and applied it to one call.

**Fix.** Put the bound inside the seam: give `WorkQueue.connect` an explicit `connect_timeout` / `max_reconnect_attempts` and wrap it in `asyncio.wait_for` there, so every caller inherits it and no future call site can forget. Give the drain a separate, larger overall deadline (it legitimately runs long) but keep the CONNECT bounded at seconds. Add a test that points `RASK_NATS_URL` at a black-hole address and asserts each activity raises within the bound rather than hanging.

**Verifier (CONFIRMED).** Verified byte-for-byte. runtime.py:192, :232, :291 are each `queue = await WorkQueue.connect(nats_url())` with no bound, inside `publish_chunk_units`, `drain_chunk_units` and `reconcile_from_queue` — the bodies of the `publish_units`, `drain_chunk` and `reconcile_chunk` activities respectively. queue.py:127-128 is `async def connect(cls, servers, **options) -> Self: nc = await…

</details>

<details><summary><b>ingest-flow-04</b> — The run-deadline path abandons its child workflows and then purges the queue underneath them <i>(ingest-flow, dapr-events, effort M)</i></summary>

**Sites:** `services/ingest/src/ingest/workflow.py:295`, `services/ingest/src/ingest/workflow.py:296`, `services/ingest/src/ingest/workflow.py:306`, `services/ingest/src/ingest/workflow.py:522`, `services/ingest/src/ingest/queue.py:273`

**Why it matters.** On the deadline path the parent declares the run FAILED while every unfinished `chunk_run` child is still durably executing — each still inside `drain_chunk`, fetching from the source and writing Lance fragments against `dataset_uri`. `release_run_units` then purges the run's subject and deletes the shared durable consumer out from under those live pull subscriptions, so in-flight acks and fetches fail against a consumer that no longer exists. The fragments those children already staged are never collected: `purge_staged` runs only inside `finalize_run`, which the timeout path deliberately skips. The net state is orphaned children still hammering a rate-limited source for a run nobody will commit, plus staged bytes with no owner — which is precisely the "unreclaimable orphan" condition `release_run` was written to prevent, reintroduced one layer up. The comment at :299-301 claims "the…

**Fix.** On the deadline (and any early-terminal) path, terminate the outstanding child workflow instances before emitting terminal — `ctx` cannot do it, so pass the child instance ids out to a `terminate_children` activity that uses `DaprWorkflowClient.terminate_workflow`, and only release the queue once they are quiescent. At minimum, make `release_run_units` a no-op when children may still be live, and make `purge_staged(uri, run_id)` run on every terminal path (not only inside `finalize_run`) so abandoned fragments are reclaimed rather than orphaned.

**Verifier (CONFIRMED).** workflow.py:295-307 is exactly as quoted: `deadline = ctx.create_timer(...)`, `winner = yield wf.when_any([fanout, deadline])`, `if winner is deadline:` -> build `timed_out` -> `yield ctx.call_activity(emit_terminal, ...)` -> `return timed_out`. Nothing anywhere terminates the outstanding `chunk_run` children — grep for `terminate_workflow` in the plane returns nothing. `emit_terminal`…

</details>

<details><summary><b>ratch-002</b> — `ratch feature topics` reads the columns a fire-and-forget Ray Job has not written yet <i>(ratch, resilience, effort M)</i></summary>

**Sites:** `packages/ratch/src/ratch/core/jobs.py:133`, `packages/ratch/src/ratch/features/columns.py:327`, `packages/ratch/src/ratch/features/topic_tree.py:102`

**Why it matters.** With `RATCH_RAY_ENABLED=1` (the documented cluster mode — `jobs.py:19-24` and `runners.py:14-16` both name it), `run_runner` returns milliseconds after `submit_job`, so `build_topic_tree` scans `chunks.lance` before the Toponymy worker has committed anything. On a first run this raises `ValueError("no topic_l* columns … run 'ratch feature topics' first")` from inside the very command that IS `feature topics` — a self-contradicting error. On a re-run over a corpus that already has `topic_l*` columns it is worse than a crash: it silently rebuilds the tree from the PREVIOUS run's stale labels and reports success, because `overwrite_dataset(topics_path, table)` (topic_tree.py:117) unconditionally overwrites. The in-process branch (`ray_enabled=False`, `jobs.py:126-128` → `_run_in_process`) blocks correctly, so the bug only appears in the mode the docstrings call the intended one — exactly…

**Fix.** The submit-and-ack ruling is fine for a mover whose downstream is woken by the commit event, but `_run_topics` is a synchronous CLI step that consumes the output directly. Either (a) split the command so the tree build is a separate invocation that the job's completion event triggers (the shape `topic_tree` already exists for — `FEATURES["topic_tree"]`, columns.py:397), and have `_run_topics` return after dispatch with a message saying so; or (b) give `run_runner` an explicit `wait: bool` (defaulting to the current behaviour) and have `_run_topics` pass `wait=True`, restoring the terminal-status poll that `_TERMINAL_STATUSES`, `ray_job_timeout_seconds` and `ray_poll_interval_seconds` are…

**Verifier (CONFIRMED).** Code matches exactly. core/jobs.py:131-137: after `_submit_or_resubmit` the A13 SUBMIT-AND-ACK comment replaced the poll and `run_runner` returns immediately; the only blocking branch is `if not settings.ray_enabled: _run_in_process(job); return` at :126-128. features/columns.py:327-328 is `run_runner(RunnerJob(runner='topics', ...))` followed directly by `return _run_topic_tree(db_path, opts,…

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `ANN-06` | med | M | Domain errors cross the actor boundary as formatted strings reconstructed by regex | `services/annotator/src/annotator/projects/proxies.py:60` |
| `ANN-10` | med | L | Multi-write create/send sequences have no compensation — a mid-sequence failure leaves an unusable project and relies on the caller retrying | `services/annotator/src/annotator/api/v1/endpoints/projects.py:105` |
| `ANN-14` | med | M | Publish transport builds a fresh httpx connection per call and retries on only one of its two paths | `services/annotator/src/annotator/projects/lakehouse.py:100` |
| `CAT-CORE-05` | med | M | Control-plane registry writes are plain overwrites with no compare-and-swap — concurrent updates are silently lost | `services/catalog/src/catalog/services/warehouses.py:78` |
| `COMPUTE-RAY-CLIENT-RETRY-STORM` | med | S | `get_ray_client` re-runs the blocking `build_client` on every request while Ray is down, with no backoff or negative caching | `services/compute/src/compute/dependencies.py:21` |
| `DUP-14` | med | S | The same hand-rolled HTTP backoff loop exists in packages/storage and services/ingest, while tenacity is the estate's retry layer | `packages/storage/src/storage/iiif.py:89` |
| `DUP-15` | med | S | The Dapr-workflow scheduler is written twice, and the two copies' timeouts have already drifted | `services/ingest/src/ingest/__init__.py:214` |
| `FLOWS-DURABLE-RUN-UNREADABLE` | med | M | A durable run reports `running` forever — the workflow's terminal state is never read back into `GET /flows/runs/{id}` | `services/flows/src/flows/routes.py:113` |
| `ING-09` | med | S | _fetch_http is a near-verbatim copy of storage.iiif.fetch_image's retry loop, assert-for-control-flow included | `services/ingest/src/ingest/fetch.py:82` |
| `ING-14` | med | M | The A8 provenance check fetches the estate's entire unbounded /runs board and linear-scans it on every status read of a completed run | `services/ingest/src/ingest/provenance.py:97` |
| `MED-006` | med | S | Deterministic HTR page failures route to RETRY, re-running every GPU transcription in the batch — the sibling media lane already… | `services/medallion/src/medallion/services/htr_stage.py:84` |
| `MED-007` | med | M | The publication trigger's `from_version`/`to_version` range is published but never read — the mover still full-overwrites the tier, so the… | `services/medallion/src/medallion/services/publication_trigger.py:112` |
| `PS-01` | med | S | `client.meta.events.unregister("needs-retry.s3")` is a proven no-op — the retry handler it means to remove is still registered | `packages/storage/src/storage/client.py:102` |
| `PS-03` | med | S | Hand-rolled retry loop with `time.sleep` and a bare `assert` in `fetch_image` | `packages/storage/src/storage/iiif.py:87` |
| `ingest-flow-05` | med | S | Transient fetch failures nak with no delay and max_deliver exhaustion is never detected — the DLQ the module docstring promises is… | `services/ingest/src/ingest/worker.py:279` |
| `ingest-flow-06` | med | S | park_poison publishes unguarded — one bad unit fails the whole run when the DLQ stream is absent | `services/ingest/src/ingest/worker.py:275` |
| `ingest-flow-07` | med | S | No trace context crosses the queue boundary; UnitTask.traceparent is a declared-but-never-written field | `services/ingest/src/ingest/queue.py:84` |
| `ingest-flow-08` | med | S | Every chunk's reconcile error uses the literal key "__chunk__", and the parent flattens all chunks into one dict — N failures collapse to… | `services/ingest/src/ingest/runtime.py:299` |
| `ingest-flow-14` | med | S | publish_units awaits one JetStream publish per unit and discards the PubAck, so a chunk is 1000 sequential round-trips and dedupes are… | `services/ingest/src/ingest/queue.py:214` |
| `ratch-006` | med | M | No retry or backoff anywhere on the model-server HTTP boundary, in a pipeline designed for hours-long runs | `packages/ratch/src/ratch/clients/base.py:43` |
| `ratch-013` | med | S | Two ffmpeg subprocesses run with no timeout while every sibling ffmpeg call sets one | `packages/ratch/src/ratch/modalities/av/thumbnails.py:51` |
| `ingest-flow-17` | low | S | ensure_stream treats every add_stream exception as "already exists" and logs it at DEBUG | `services/ingest/src/ingest/queue.py:148` |
| `ratch-019` | low | M | `_gate_filter` inlines every admitted doc id into one unbounded SQL `IN` list | `packages/ratch/src/ratch/core/driver.py:184` |

### E4 — Error contract: one taxonomy, no fail-open, no silent swallow

**P1** · 40 issues (4 high, 29 medium, 7 low)

Three separate defects share one root: routes that invent their own error shape, guards that fail OPEN on infrastructure failure, and `except Exception` blocks that turn an outage into an empty result.

#### The high-severity items in this epic

<details><summary><b>FLOWS-NODE-ESCAPE</b> — `run_node` catches only `NodeError`, so a bad `regexReplace` 500s the entire run and orphans its sibling nodes <i>(flows-fleet, error-handling, effort S)</i></summary>

**Sites:** `services/flows/src/flows/executor.py:273`, `services/flows/src/flows/executor.py:249`, `services/flows/src/flows/executor.py:327`, `services/flows/src/flows/activities.py:40`

**Why it matters.** The stated contract of this module is "failure captured as state rather than raised" so the builder can paint one node red — a design `test_a_failing_model_node_blocks_its_dependents` asserts explicitly. Any exception that is not a `NodeError` silently violates it, and `regexReplace` is a plain text field in the published catalog, so this is reachable by typing. `asyncio.gather` without `return_exceptions=True` also leaves the wave's other node coroutines running detached with their results discarded and their in-flight Serve POSTs still open. On the durable lane the same escape raises out of the activity, burns all three `NODE_RETRY` attempts on a deterministic failure, and then fails the whole workflow.

**Fix.** Wrap the substitution in the same `except re.error -> NodeError` guard the compile already has, and make the escape structurally impossible: broaden `run_node`'s handler to `except Exception as exc` — logging it at exception level and returning a failed `NodeRunState` with a stable message — since a node failing is a normal outcome and an unexpected exception inside one node must not be able to abort the run. Pass `return_exceptions=True` to the `gather` so a wave always resolves.

**Verifier (CONFIRMED).** executor.py:264-274: run_node's try covers `await dispatch(...)` and catches ONLY NodeError. _regex's try (:245-248) wraps re.compile alone; the substitution at :251 (`compiled.sub(replacement..., payload.text)`) is outside it. Reproduced: re.compile(r'(\d+)').sub(r'\g<9>','1723') raises re.PatternError('invalid group reference 9 at position 3'); verified re.PatternError IS re.error (same…

</details>

<details><summary><b>GW-URL-DECODE</b> — Gateway rebuilds the upstream URL from the percent-DECODED path, corrupting or truncating it (and 500-ing on some inputs) <i>(flows-fleet, error-handling, effort M)</i></summary>

**Sites:** `services/gateway/src/gateway/__init__.py:315`, `services/gateway/src/gateway/__init__.py:324`, `services/gateway/src/gateway/__init__.py:325`, `services/gateway/src/gateway/__init__.py:177`

**Why it matters.** A proxy must forward the path the caller sent. Here the gateway routes and 403-guards on the full decoded path but hands the upstream a DIFFERENT, shorter one — so a request can be authorized against one resource and executed against another. `%1F` is worse than wrong: `httpx.InvalidURL` is not a `RequestError`, so it misses the 502 handler at line 330 and escapes as an unhandled 500 from wholly client-controlled input. The Lance Namespace spec's canonical identifier delimiter is the unit separator (rask's catalog currently configures `$` via LANCE_NS_DELIMITER, which is what keeps this latent rather than live), and any path parameter carrying an encoded '/', '?' or '#' — annotator doc/speech/chunk ids, future object keys — hits the truncation today.

**Fix.** Route on `_normalize_path(request.url.path)` as now, but BUILD the upstream URL from `request.scope["raw_path"]` (falling back to the encoded form of `request.url.path`) so the wire path round-trips byte-for-byte: slice the raw bytes by the raw-prefix length rather than the decoded one, and construct with `httpx.URL(base).copy_with(raw_path=...)` instead of f-string concatenation, which re-parses ':', '?' and '#' as delimiters. Add `httpx.InvalidURL` to the guarded exceptions so a malformed identifier is a 400, never a 500. Pin all four reproductions above as tests.

**Verifier (CONFIRMED).** Code matches: services/gateway/src/gateway/__init__.py:315 norm_path=_normalize_path(request.url.path), :324 upstream_path=upstream_prefix+norm_path[len(route_prefix):], :325 httpx.URL(f"{base}{upstream_path}").copy_with(query=...). scope["raw_path"] is never read anywhere in the module (grep: zero hits). I reproduced the URL-building directly against the real _normalize_path with…

</details>

<details><summary><b>SKG-01</b> — Idempotency of tuple writes/deletes is decided by substring-matching the OpenFGA error body, so a genuinely rejected write or revoke is swallowed… <i>(service-kit-governed, error-handling, effort M)</i></summary>

**Sites:** `packages/service-kit/src/service_kit/governed/fga.py:105`, `packages/service-kit/src/service_kit/governed/fga.py:109`, `packages/service-kit/src/service_kit/governed/fga.py:977`, `packages/service-kit/src/service_kit/governed/fga.py:1150`, `packages/service-kit/src/service_kit/governed/fga.py:1079` *(+1 more)*

**Why it matters.** `write_failed_due_to_invalid_input` appears in BOTH marker lists, so the same code is asked to mean two opposite things, and the bare substring `"does not exist"` also matches OpenFGA's undefined-relation/undefined-type validation text. That is exactly the hazard the module's own `grant_on_create` docstring warns about at :1134 (`parent` vs `tenant` — "a tuple OpenFGA accepts and no rule ever reads"): a delete carrying a mis-spelled relation is classified "already absent", skipped, and then emitted as an `access_tuple_delete` SUCCESS audit row. The live grant survives while the compliance trail says it was revoked — the stale-grant privilege bleed `revoke_object_tuples`'s own docstring (:1208) exists to prevent. On the write side a swallowed rejection leaves an object with no owner while `grant_on_create` returns success, which is the incident already recorded at :1038-1043.

**Fix.** Stop inferring idempotency from prose. openfga_sdk ships the supported primitive: pass `options={"conflict": ConflictOptions(on_duplicate_writes=ClientWriteRequestOnDuplicateWrites.IGNORE, on_missing_deletes=ClientWriteRequestOnMissingDeletes.IGNORE)}` to `client.write(...)` so the SERVER decides, and let every remaining 400 propagate as a real failure. If a client-side classifier must stay for older servers, key it on the structured `exc.code`/`error_message` field only (never `str(exc)`), require an exact code match, and add tests that a 400 with any other validation message is NOT swallowed. Either way, never `_audit_tuples` a tuple whose write/delete was not confirmed.

**Verifier (CONFIRMED).** Verified verbatim. fga.py:105 `_DUPLICATE_WRITE_MARKERS = ("already exists", "write_failed_due_to_invalid_input")` and fga.py:109 `_MISSING_DELETE_MARKERS = ("cannot delete", "does not exist", "write_failed_due_to_invalid_input")` — the same OpenFGA code is in BOTH lists, and both classifiers (`_is_duplicate_write` :977-982, `_is_missing_delete` :1150-1155) lower-case `str(exc) +…

</details>

<details><summary><b>catalog-api-01</b> — endpoints/stores.py raises service_kit.exceptions, which bypass the catalog's RFC 9457 problem handler entirely <i>(catalog-api, error-handling, effort S)</i></summary>

**Sites:** `services/catalog/src/catalog/api/v1/endpoints/stores.py:30`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:59`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:98`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:100`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:105` *(+1 more)*

**Why it matters.** Every other error in this service carries the spec's numeric `code` and the `application/problem+json` media type; generated Lance-Namespace clients dispatch on that `code`. Three routes in `/v1/stores` answer a differently-shaped body with no `code` at all, so a client that parses the catalog's documented error model gets a parse failure or a silent fall-through on exactly the conflict/unavailable paths it must handle. It also fires the default FastAPI `HTTPException` handler, which is the one path `install_problem_handlers` was written to remove.

**Fix.** Replace the three `service_kit.exceptions` raises with their `lance_namespace` equivalents (`NamespaceAlreadyExistsError` for the duplicate-name conflict, `InvalidInputError` for the missing name/bucket, `ServiceUnavailableError` from `lance_namespace` for the unreadable/absent state store) and drop the `service_kit.exceptions` import. Add a contract test asserting `content-type == application/problem+json` and a present `code` on a duplicate `POST /v1/stores`.

**Verifier (CONFIRMED).** Verified at the exact lines. stores.py:30 imports ConflictError/ServiceUnavailableError/ValidationError from service_kit.exceptions and raises them at :59, :98, :100, :105. service_kit/exceptions.py:45 `class DomainError(HTTPException)`, and its module docstring (lines 24-28) explicitly states the lakehouse services (catalog included) use the lance_namespace taxonomy because the spec pins a…

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `ANN-05` | med | M | Ontology enforcement fails open on the two BULK-WRITE paths (send + import) when a stored ontology will not parse | `services/annotator/src/annotator/api/v1/endpoints/project_events.py:368` |
| `CAT-CORE-06` | med | S | The model registry collapses every `OSError` to 404, reporting a store outage as "model not found" | `services/catalog/src/catalog/services/models.py:47` |
| `COMPUTE-PROXY-CONTENT-ENCODING` | med | S | The Serve proxy forwards `content-encoding`/`content-length` from an upstream body httpx has already decompressed | `services/compute/src/compute/proxy.py:45` |
| `CP-503-NEVER-FIRES` | med | S | The controlplane's designed 503 cannot fire for its likeliest cause — `load_kube_config()` raises inside the dependency, outside the… | `services/controlplane/src/controlplane/routes.py:25` |
| `F-LIN-11` | med | S | Three un-logged `except Exception` swallows in demo.py beside an unused logger, and a BaseException catch that misreports MemoryError as… | `services/lineage/src/lineage/api/v1/endpoints/demo.py:31` |
| `FLOWS-BROAD-EXCEPT` | med | S | Two unbounded `except Exception` blocks make a code defect indistinguishable from an absent sidecar | `services/flows/src/flows/lifespan.py:61` |
| `GW-NO-PROBLEM-JSON` | med | S | The gateway is the only service in the fleet that does not answer RFC 9457 problem+json | `services/gateway/src/gateway/__init__.py:266` |
| `ING-03` | med | M | One router, two incompatible error contracts: four domain HTTPExceptions bypass the problem+json translator this app installs | `services/ingest/src/ingest/api.py:130` |
| `ING-06` | med | S | The same list loop swallows an FGA/authn outage as if it were a denial — an incident renders as an empty list at DEBUG | `services/ingest/src/ingest/api.py:249` |
| `ING-10` | med | S | The 202's Location header points at /v1/ingests/{run_id} — a path this service never serves under any prefix | `services/ingest/src/ingest/api.py:155` |
| `ING-12` | med | M | authorize_ingest is a pseudo-dependency: its Header/Depends annotations are inert because all three call sites invoke it positionally… | `services/ingest/src/ingest/auth.py:98` |
| `MAINT-01` | med | S | The documented compact→cleanup→optimize order invariant is not the order the code runs (two docstrings assert a sequence the… | `services/maintenance/src/maintenance/services/optimize.py:120` |
| `MAINT-05` | med | M | Unguarded S3/Lance calls in the orphan category can raise out of reconcile() and 500 the entire tick, discarding the seven store… | `services/maintenance/src/maintenance/services/reconcile.py:653` |
| `MAINT-06` | med | S | The branch / MemWAL layout gate swallows its probe error and continues, failing OPEN on the one check that exists to stop live files being… | `services/maintenance/src/maintenance/services/orphans.py:275` |
| `MAINT-08` | med | S | reconcile()'s control_root default falls back to the POLICY root, not the control root — the exact root-confusion the sweep documents as a… | `services/maintenance/src/maintenance/services/reconcile.py:701` |
| `PS-02` | med | M | storage's own error taxonomy is half-applied: `s3_errors` wraps nothing inside `packages/storage`, and `iiif.py` re-implements it inline | `packages/storage/src/storage/iiif.py:197` |
| `PS-17` | med | S | `logs()` reports `ok=True, "(empty or unavailable)"` for every status ≥ 400 — a 401 from a token-authed dashboard renders as an empty log… | `packages/ray-kit/src/ray_kit/dashboard.py:538` |
| `PS-18` | med | S | `job_logs` catches bare `Exception` while its six siblings catch `RAY_TRANSIENT_ERRORS` | `packages/ray-kit/src/ray_kit/dashboard.py:516` |
| `SK-02` | med | S | `LocalCatalogWriteTransport` (dev/offline catalog fallback) omits the commit-conflict translation the direct path has — 500 instead of 409 | `packages/service-kit/src/service_kit/lancekit/writer.py:141` |
| `SK-04` | med | S | Errors are classified by substring-matching exception messages in three separate places | `packages/service-kit/src/service_kit/lancekit/registry.py:157` |
| `SK-05` | med | M | A transient per-table read failure is laundered into a 404 "dataset not found" | `packages/service-kit/src/service_kit/lancekit/introspect.py:97` |
| `SKG-04` | med | M | Two silent-truncation paths surface only to the log; the return value is indistinguishable from a complete answer | `packages/service-kit/src/service_kit/governed/fga.py:547` |
| `SKG-05` | med | M | governed/deps.py raises the FLEET exception taxonomy while its three sibling modules raise the Lance one — a latent off-contract 401/503… | `packages/service-kit/src/service_kit/governed/deps.py:38` |
| `VS-06` | med | M | Broad `except Exception` re-raised as `ValidationError` turns infrastructure faults into HTTP 400 client errors | `services/search/src/search/services/vector.py:57` |
| `VS-07` | med | M | Five silent swallows in the search path hide real failures as empty results (three of the nine cited sites do log) | `services/search/src/search/services/service.py:298` |
| `VS-11` | med | S | `_creds`' documented fail-closed handler is unreachable, so a down secret store reports "the secret is empty" | `services/viewer/src/viewer/api/v1/endpoints/objects.py:86` |
| `VS-14` | med | S | Rerank scores are silently misaligned with candidates when the server returns a short or sparse result list | `services/search/src/search/services/encoders/reranker.py:69` |
| `catalog-api-10` | med | S | Two sibling access surfaces map the same client error to different spec codes — 501 UnsupportedOperation vs 400 InvalidInput for an… | `services/catalog/src/catalog/api/v1/endpoints/access.py:147` |
| `ratch-010` | med | S | Index-build failures are swallowed at DEBUG and one helper is a literal `except Exception: pass` | `packages/ratch/src/ratch/ingest/ingest.py:433` |
| `FLOWS-422-BYPASSES-HIERARCHY` | low | M | `create_run` hand-builds a problem+json body and returns `RunState \| JSONResponse` because the shared exception hierarchy cannot carry… | `services/flows/src/flows/routes.py:66` |
| `SK-20` | low | S | Bare `except Exception` swallow-and-continue in three shared code paths | `packages/service-kit/src/service_kit/lancekit/introspect.py:97` |
| `SKG-15` | low | S | fetch_dapr_secret swallows every exception into an empty bundle and logs a boot-blocking failure at WARNING | `packages/service-kit/src/service_kit/governed/secrets.py:74` |
| `X11` | low | S | ingest installs both handler sets, so its 422 body silently differs from its three fleet siblings | `services/ingest/src/ingest/__init__.py:47` |
| `catalog-api-07` | low | S | _collect_descendants recurses with no depth cap and no cycle guard, while its sibling enumerator in the same service has both | `services/catalog/src/catalog/api/v1/endpoints/namespaces.py:54` |
| `catalog-api-08` | low | S | Bare `except Exception` around seed_ownership makes any programming error in the grant path look like an FGA outage | `services/catalog/src/catalog/api/v1/endpoints/data.py:258` |
| `ingest-flow-16` | low | S | Generator workflows annotated as returning their final value, plus Any seams and a stale type-ignore that the QueueMessage Protocol… | `services/ingest/src/ingest/workflow.py:170` |

### E5 — One settings object per service; no scattered env reads

**P1** · 29 issues (2 high, 18 medium, 9 low)

97 raw `os.getenv` / `os.environ` reads live outside settings modules; two of the busiest services (gateway, ingest) have no `Settings` class at all, and one Dapr secret store answers to seven env names.

#### The high-severity items in this epic

<details><summary><b>F-LIN-02</b> — The Dapr secret store is addressed by two different env-var names — the settings one is ignored on the auth path <i>(lineage, config, effort S)</i></summary>

**Sites:** `services/lineage/src/lineage/api/security.py:85`, `services/lineage/src/lineage/api/security.py:86`, `services/lineage/src/lineage/core/config.py:144`, `services/lineage/src/lineage/core/config.py:145`

**Why it matters.** Failure scenario: an operator points the estate at a differently-named store with the documented `LINEAGE_DAPR_SECRET_STORE=prod-secrets`. `apply_dapr_secrets()` (config.py:215) correctly reads `prod-secrets`, so boot succeeds and S3/DB secrets land. `_dedicated_token()` still queries the hardcoded default `lance-secrets`, gets `{}` back, returns `None` — and every request from a subject listed in `LINEAGE_PRIVILEGED_SUBJECTS` is refused with `UnauthenticatedError("...is privileged but has no dedicated credential provisioned")` (security.py:124). The deployment looks correctly configured; the privileged lane is dead. This also violates HOUSE-RULE-9 outright: three `os.environ` reads live outside the settings module (`APP_API_TOKEN` at security.py:106 is the third).

**Fix.** Delete `_dedicated_token`'s two `os.environ.get` calls and pass `settings.dapr_secret_store` / `settings.dapr_secret_key` down from `_service_principal` (which already receives `SettingsDep`). Promote `APP_API_TOKEN` to a `LineageSettings` field too, so the whole auth path reads one config object. If the two stores are genuinely meant to differ, they need two distinctly-named settings fields with that intent documented.

**Verifier (CONFIRMED).** Exact. config.py:144-145 declare `dapr_secret_store`/`dapr_secret_key` under LINEAGE_DAPR_SECRET_STORE/LINEAGE_DAPR_SECRET_KEY; security.py:85-86 read the differently-named LINEAGE_SECRET_STORE/LINEAGE_SECRET_KEY straight from os.environ. Repo-wide grep across .py/.yaml/.yml/.sh/.tpl/.md returns exactly those four lines — nothing in the chart, scripts, or compose sets the security.py pair, so it…

</details>

<details><summary><b>ING-04</b> — s3-prefix builds a bare pyarrow S3FileSystem that ignores every RASK_S3_* knob — while its docstring claims it goes through the estate's… <i>(ingest-domain, config, effort M)</i></summary>

**Sites:** `services/ingest/src/ingest/adapters.py:101`, `services/ingest/src/ingest/adapters.py:115`

**Why it matters.** `s3-prefix` is the plane's primary non-IIIF source and the whole reason the registry exists (`adapters.py:3-8`: it "sat written-and-unwired for months"). In-cluster it points at RustFS via `RASK_S3_ENDPOINT_URL`; this construction ignores that and resolves to real AWS unless the caller hand-types `options.endpoint` on every single request — and even then `endpoint_override` alone drops the scheme/TLS handling `objectfs.s3_filesystem` was written for (its docstring: "hardcoding `http` once silently downgraded a secured connection"). The docstring asserting the opposite is worse than silence: a reader auditing the MinIO-agnostic rule greps for the claim, finds it, and moves on.

**Fix.** Build the filesystem through `service_kit.lakehouse.objectfs.s3_filesystem(...)` seeded from the same env `packages/storage.client` reads (endpoint, insecure, CA bundle, region), keeping `options.endpoint` as a per-request override. Correct the docstring to name what it actually calls. Add a moto-backed adapter test that asserts the endpoint comes from `RASK_S3_ENDPOINT_URL` with no `options.endpoint`.

**Verifier (CONFIRMED).** adapters.py:101-116 verified verbatim: the docstring claims 'over the estate's provider-agnostic client' and '`storage.s3_client` rather than boto3 directly', and the body imports `pyarrow.fs` and builds `pafs.S3FileSystem(endpoint_override=str(endpoint)) if endpoint else pafs.S3FileSystem()`. `storage.s3_client` is never imported in the file. The canonical builder exists and is used elsewhere:…

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `ANN-04` | med | S | CORS `allow_methods` omits PUT/PATCH/DELETE while seven routes serve exactly those methods | `services/annotator/src/annotator/main.py:125` |
| `CAT-CORE-13` | med | M | A single 340-line `Settings` class carries every domain's configuration | `services/catalog/src/catalog/core/config.py:21` |
| `DUP-08` | med | M | The OIDC/FGA settings block is re-declared in 4 services despite `GovernedAuthSettings` existing for it | `services/catalog/src/catalog/core/config.py:175` |
| `DUP-11` | med | S | The catalog identifier delimiter `$` is declared nine times through three different mechanisms | `services/catalog/src/catalog/core/config.py:29` |
| `DUP-17` | med | M | One Dapr secret store is named by seven different env vars sharing one default | `services/catalog/src/catalog/core/config.py:169` |
| `FLEET-ENV-SCATTER` | med | M | 19 raw `os.environ` reads outside any settings module — the gateway has no Settings class at all, and several reads happen per request | `services/gateway/src/gateway/__init__.py:109` |
| `ING-07` | med | M | No service settings model: 25 scattered os.getenv reads across 8 files, several frozen at import time, with one convention read three… | `services/ingest/src/ingest/fetch.py:43` |
| `ING-13` | med | S | get_auth_settings is uncached, so every request re-instantiates a BaseSettings that reads .env from disk — and then hand-patches a field… | `services/ingest/src/ingest/auth.py:72` |
| `PS-05` | med | S | `derive_hcp_creds` mutates process-global `os.environ` to inject S3 credentials from env secrets | `packages/storage/src/storage/client.py:43` |
| `PS-06` | med | S | `packages/storage` declares `requires-python = ">=3.10"` but uses PEP 695 `type` syntax (3.12+) — the metadata is factually wrong | `packages/storage/pyproject.toml:5` |
| `SK-08` | med | M | `make_service_app` reads `.env` and builds `Settings` at import time; settings are not injectable | `packages/service-kit/src/service_kit/__init__.py:104` |
| `SKG-10` | med | M | Five direct os.environ reads outside any Settings class, three on the per-request path, plus an unprefixed env var and a hard-coded Dapr… | `packages/service-kit/src/service_kit/governed/dapr_auth.py:45` |
| `X10` | med | L | Seven env-var prefixes across twelve services, with `LANCE_*` leaking into three services that otherwise use their own | `packages/service-kit/src/service_kit/config.py:26` |
| `X3` | med | S | `known-first-party` names 8 of the 19 real first-party import names — 11 packages sort into the third-party block | `pyproject.toml:153` |
| `X7` | med | M | gateway and ingest have no Settings class — 46 of the estate's 67 raw env reads live in those two services, several captured at import time | `services/gateway/src/gateway/__init__.py:109` |
| `ingest-flow-09` | med | M | Operational tunables read via scattered os.getenv with no settings singleton — the plane already uses pydantic-settings for auth but not… | `services/ingest/src/ingest/runtime.py:145` |
| `ratch-007` | med | M | Three env-var namespaces, ten import-time `os.getenv` reads (plus two in-function), and no cached settings accessor | `packages/ratch/src/ratch/clients/embedding.py:26` |
| `ratch-014` | med | S | The library's FTS default is English while the CLI, the reindex path and the engine all default Swedish | `packages/ratch/src/ratch/ingest/ingest.py:357` |
| `MED-009` | low | S | `APP_API_TOKEN` is read raw from `os.environ` in two unrelated modules instead of the typed settings surface (the other 11 reads are OTel… | `services/medallion/src/medallion/services/ray_submit.py:74` |
| `PS-07` | low | M | storage resolves env by hand (`os.getenv` tuples) instead of pydantic-settings — and lineage-kit's docstring cites storage as an example… | `packages/storage/src/storage/client.py:23` |
| `PS-25` | low | S | `LineageSettings()` is re-instantiated on every run-open in three lineage-kit paths — no cached accessor | `packages/lineage-kit/src/lineage_kit/stage.py:56` |
| `SK-10` | low | M | Name collision inside one package: two `Settings` classes and two `get_settings` with different DI shapes (`RASK_*` vs `MEDIA_*`) | `packages/service-kit/src/service_kit/config.py:17` |
| `SK-14` | low | S | `RASK_*` env vars read directly via `os.environ` outside the settings modules | `packages/service-kit/src/service_kit/__init__.py:33` |
| `SK-18` | low | S | Constrained settings validated by hand-rolled `field_validator`s instead of `StrEnum`/`Literal` | `packages/service-kit/src/service_kit/media/config.py:115` |
| `VS-24` | low | S | `os.getenv` read for the secret-store name outside the settings module, and a private constant imported across modules | `services/viewer/src/viewer/api/v1/endpoints/objects.py:84` |
| `catalog-api-16` | low | S | Constrained string values are passed as bare `str \| None` and re-parsed with ad-hoc .lower() comparisons instead of StrEnum | `services/catalog/src/catalog/api/v1/endpoints/data.py:122` |
| `catalog-api-17` | low | S | The lifespan mutates the @lru_cache'd Settings singleton in place to inject the S3 secret | `services/catalog/src/catalog/main.py:80` |

### E6 — Unify what is copy-pasted across services

**P2** · 42 issues (5 high, 25 medium, 12 low)

The estate has a platform library and does not use it consistently: the governed-auth bootstrap is pasted into 8 lifespans, the service-door authenticator exists twice and has already diverged, and there is no factory for the lance-service entrypoint that 8 services hand-assemble.

#### The high-severity items in this epic

<details><summary><b>DUP-01</b> — The governed-auth bootstrap (OIDC verifier + FGA provision/make_client) is copy-pasted into 8 lifespans <i>(cross-service, duplication, effort M)</i></summary>

**Sites:** `services/catalog/src/catalog/main.py:89`, `services/lineage/src/lineage/main.py:86`, `services/viewer/src/viewer/main.py:52`, `services/annotator/src/annotator/main.py:54`, `services/medallion/src/medallion/producer.py:71` *(+3 more)*

**Why it matters.** The estate has already paid for this twice, and both incidents are written into the comments. medallion/producer.py:74 records that 'the medallion alone re-provisioned on every boot, minting a model version per pod restart'; ingest/__init__.py:100-116 records that ingest was the single door missing split-horizon discovery, so every signed-in ingest died with a ConnectError surfaced as `{"message":"Internal Error"}`. Eight copies of an authorization-bootstrap mean a fix lands in one and the other seven keep the bug; the eight already differ in whether construction failures are fatal (catalog/lineage raise, viewer/annotator swallow into a logged 503, maintenance returns None, ingest defers to a resolve-by-name path).

**Fix.** Add `service_kit.governed.bootstrap.wire_governed_auth(app, settings, *, provision: bool = True) -> None` to service-kit (it already owns `fga`, `oidc` and `GovernedAuthSettings`, and takes no new dependency). It should own the pinned-else-provision decision, the split-horizon override, the timeout, and the failure posture as an explicit parameter — `provision=False` for the read-only consumers (ingest, maintenance) that must never author a model, matching the `fga.resolve` vs `fga.provision` split that already exists at packages/service-kit/src/service_kit/governed/fga.py:278. Then delete the eight blocks. service-kit base stays lance/ray-free: `fga`/`oidc` are already under `governed/`.

</details>

<details><summary><b>DUP-02</b> — lineage re-implements service_kit's service-door authenticator, and the two copies have already diverged <i>(cross-service, duplication, effort M)</i></summary>

**Sites:** `packages/service-kit/src/service_kit/governed/dapr_auth.py:143`, `services/lineage/src/lineage/api/security.py:90`, `services/catalog/src/catalog/api/security.py:96`

**Why it matters.** This is the estate's service-to-service authentication primitive — the one that decides whether a caller may claim `service-trainer` (writer on `namespace:models`). Its own docstrings record that the lessons it encodes 'were each paid for once and must not be re-learned per service'. With two copies, they are being re-learned: the entry-guard divergence means a caller supplying `x-lance-service-identity: anything` gets a hard 403 from lineage where the catalog falls through to OIDC, and any future hardening (a new privileged-subject rule, a new laundering path) must be found in two files rather than one. A security control with a second copy is a security control with a stale copy.

**Fix.** Delete `lineage/api/security.py::_service_principal` and `ServicePrincipal`; call `service_kit.governed.dapr_auth.service_principal(...)` as catalog/api/security.py:96 already does, passing `dedicated_token=_dedicated_token`. The only real difference is the exception taxonomy (lineage raises `lance_namespace` typed errors, the shared helper raises `HTTPException`) — resolve it by having the shared helper raise the `lance_namespace` types, since every consumer of this door installs `service_kit.lakehouse.ns_errors.install_problem_handlers` anyway.

</details>

<details><summary><b>DUP-03</b> — `authorize_produce` and `authorize_ingest` are two ~120-line copies of one dual-auth door <i>(cross-service, duplication, effort M)</i></summary>

**Sites:** `services/medallion/src/medallion/api/produce_auth.py:41`, `services/medallion/src/medallion/api/produce_auth.py:58`, `services/ingest/src/ingest/auth.py:81`, `services/ingest/src/ingest/auth.py:98`

**Why it matters.** The docstring's own reasoning — 'two different answers to one question is how an estate ends up with a weak door and a strong one' — is the argument for sharing, not for copying. Both doors gate write paths into a project's tiers, both encode the measured gateway-laundering bypass (produce_auth.py:86-91 cites the ingest measurement: '403 straight to the pod, 202 through the gateway'), and both must be fixed together forever. The order of checks already differs: produce_auth refuses the public caller BEFORE the token compare (line 93), ingest/auth compares the token first and refuses after (lines 126/135), so a request carrying both a valid service token and an Authorization header takes different paths through the two doors.

**Fix.** Extract `service_kit.governed.project_door.authorize_project_write(...)` taking `(request, *, expected_token, dapr_api_token, authorization, dapr_caller_app_id, project, service_project, oidc_enabled, fga_client, action="ingest"|"produce")` and returning None or raising the typed errors. Both call sites shrink to a settings-binding wrapper, exactly as `service_kit.governed.deps.make_auth_deps` already does for the read-side checker. Keep the `action` string only for the audit label and the denial message.

</details>

<details><summary><b>DUP-04</b> — catalog's `_bucket_client` builds boto3 directly, bypassing packages/storage and losing s3v4, path-style and timeouts <i>(cross-service, duplication, effort S)</i></summary>

**Sites:** `services/catalog/src/catalog/services/warehouses.py:41`, `services/catalog/src/catalog/core/vending.py:195`, `services/catalog/src/catalog/core/vending.py:251`, `packages/storage/src/storage/client.py:73`

**Why it matters.** Two independent problems. (1) HOUSE-RULE-8: `packages/storage.s3_client` is the canonical wrapper and service code must not import boto3 — maintenance already complies for the same operation, so this is drift, not a considered exception. (2) The omitted `addressing_style: "path"` is the exact configuration `service_kit/lakehouse/objectfs.py:32` warns about — 'RustFS/MinIO reject virtual-hosted signing with 403 SignatureDoesNotMatch' — and boto3's default `auto` prefers virtual-hosted addressing for DNS-compatible bucket names, which is precisely what a warehouse bucket is. The missing connect/read timeouts also violate HOUSE-RULE-10 on a blocking call that runs in a threadpool during warehouse create/delete.

**Fix.** Replace `_bucket_client` with `storage.s3_client(storage_options["endpoint"], access_key=..., secret_key=...)`. If the vending paths genuinely need an STS client rather than an S3 one (vending.py:195/251 do), leave them but move the STS-client construction behind a named helper in `packages/storage` too, so `import boto3` appears in exactly one package.

</details>

<details><summary><b>ratch-004</b> — The library reaches into `runners/*` and three `services/*` modules that are not — and by ruling cannot be — its dependencies <i>(ratch, coupling, effort L)</i></summary>

**Sites:** `packages/ratch/src/ratch/cli/transcribe.py:61`, `packages/ratch/src/ratch/cli/transcribe.py:127`, `packages/ratch/src/ratch/cli/speaker.py:22`, `packages/ratch/src/ratch/cli/speaker.py:85`, `packages/ratch/src/ratch/cli/speaker.py:238` *(+5 more)*

**Why it matters.** The root `pyproject.toml:26-29` states the ruling plainly: "NOTE: runners/* are deliberately NOT members — sealed model envs whose heavy pins (CUDA torch, model SDKs) must never enter the services' resolution", and `runners/asr/pyproject.toml` pins `torch==2.11.0+cu128` behind a private index with `requires-python = ">=3.10,<3.13"` — incompatible with ratch's `>=3.13`. So `ratch transcribe` and `ratch detect-language` can never import successfully from a correctly-provisioned ratch env; they raise a bare `ModuleNotFoundError` traceback rather than the actionable `RunnerJobError` that `core/jobs.py:213-218` was written to give ("runner {job.runner!r} is not importable here … Run it in its sealed env"). The dependency is also circular: `runners/diarize/diarize.py:32` does `from ratch.modalities.av.wav import extract_wav_16k_mono` while `cli/speaker.py:85` imports back the other way.…

**Fix.** Route every runner through the seam that already exists and already produces a good error: `core.jobs.run_runner(RunnerJob(runner="asr", …))`, exactly as `_run_topics` does for `topics` — deleting the direct `from runners.… import` lines in `cli/transcribe.py` and `cli/speaker.py`. That also removes the circular edge, since `runners.*` may keep importing `ratch`. Replace `runners_root()`'s `parents[5]` with an explicit, overridable setting on `RunnersSettings` (e.g. `runners_root: Path` from `RATCH_RUNNERS_ROOT`, defaulting to the walk) so an installed wheel fails loudly instead of pointing at `/venv`. Drop `cmd_serve` entirely — three services are started by `scripts/dev-micro.sh`, which…

**Verifier (CONFIRMED).** Every cited import exists: cli/transcribe.py:61 `from runners.asr.transcribe import run_transcribe` (comment claims a '[transcribe]' extra that pyproject does not declare — only `atlas`), :127 `from runners.asr.detect_language import detect_and_sort`; cli/speaker.py:22 (TYPE_CHECKING), :85, :238-245, :365 all import `runners.diarize` / `runners.voiceprint`; cli/search.py:94 Popens…

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `ANN-13` | med | M | Route prefixes are inconsistent inside one service — `/api`, `/api/jobs` and bare `/projects` / `/tasks` | `services/annotator/src/annotator/annotations/router.py:14` |
| `CAT-CORE-07` | med | M | An 11-keyword emitter signature is hand-copied seven times across the lineage emission layer | `services/catalog/src/catalog/core/lineage_emit.py:278` |
| `CAT-CORE-11` | med | S | The warehouse registry hand-rolls a raw boto3 S3 client instead of the canonical `packages/storage` wrapper | `services/catalog/src/catalog/services/warehouses.py:35` |
| `DUP-05` | med | S | `control_emit.py` exists twice, near-verbatim (136 and 141 lines), and only one copy is tested | `services/catalog/src/catalog/core/control_emit.py:41` |
| `DUP-06` | med | S | Four byte-identical `health.py` liveness routers | `services/compute/src/compute/health.py:16` |
| `DUP-07` | med | S | The Arrow-IPC-stream encoder is hand-written at 7 sites, and the media-type string at 6 | `packages/service-kit/src/service_kit/lancekit/writer.py:151` |
| `DUP-09` | med | M | `apply_dapr_secrets` is written four times, once inline in a lifespan | `services/lineage/src/lineage/core/config.py:204` |
| `DUP-10` | med | L | Two OpenLineage kernels and four RunEvent builders, one of which documents itself as a duplicate | `packages/service-kit/src/service_kit/openlineage.py:30` |
| `DUP-12` | med | L | The lance-service entrypoint is hand-assembled eight times; there is no `make_lance_service_app` | `services/catalog/src/catalog/main.py:178` |
| `DUP-13` | med | S | annotator re-implements `service_kit.governed.deps.make_auth_deps`, the module extracted from it | `services/annotator/src/annotator/api/security.py:43` |
| `DUP-16` | med | M | viewer, search and annotator share a copy-pasted media-service lifespan | `services/viewer/src/viewer/main.py:34` |
| `MAINT-10` | med | M | build_report repeats the same guard/else block seven times, and two categories pass the same reason pair to _first in opposite order | `services/maintenance/src/maintenance/services/reconcile.py:550` |
| `MED-005` | med | S | The "build a FAIL RunEvent and publish it through the outbox" block is copy-pasted four times inside `handle_stage` | `services/medallion/src/medallion/services/transform.py:401` |
| `MED-012` | med | S | `htr_stage` imports four private names out of `compute`, coupling the HTR lane to the generic compute's internals | `services/medallion/src/medallion/services/htr_stage.py:27` |
| `PS-04` | med | M | Three copies of the lazy-client/pickle dance and four copies of the `list_objects_v2` paginate loop, with no `Source`/`Sink` Protocol to… | `packages/storage/src/storage/s3.py:49` |
| `PS-24` | med | M | The parent-resolution + namespace-defaulting ladder is copy-pasted in three modules | `packages/lineage-kit/src/lineage_kit/stage.py:46` |
| `SKG-06` | med | L | The three-field retry triple and the fail-closed except block are copy-pasted through all thirteen FGA operations | `packages/service-kit/src/service_kit/governed/fga.py:361` |
| `SKG-08` | med | L | Four hand-rolled object-store record registries with byte-identical hashed-key helpers and identical list-with-broad-except bodies | `packages/service-kit/src/service_kit/lakehouse/protection.py:40` |
| `SKG-13` | med | M | lakehouse.sources.S3Source / sinks.S3Sink shadow the canonical storage.S3Source / storage.S3Sink by name with an incompatible API | `packages/service-kit/src/service_kit/lakehouse/sources.py:74` |
| `VS-19` | med | S | Encoder construction picks its kwargs by runtime signature introspection, to accommodate test doubles | `services/search/src/search/services/clients.py:35` |
| `catalog-api-04` | med | S | Endpoint modules import each other's private helpers, and one imports a private helper out of api/dependencies | `services/catalog/src/catalog/api/v1/endpoints/tables.py:50` |
| `catalog-api-09` | med | M | The estate-gate / audit-on-outage preamble is hand-copied across eleven handlers and reaches into app.state.fga instead of using the… | `services/catalog/src/catalog/api/v1/endpoints/access.py:90` |
| `ingest-flow-11` | med | M | The catalog seam is duck-typed by hasattr/getattr while a Protocol exists that describes a different, smaller contract | `services/ingest/src/ingest/runtime.py:393` |
| `ratch-009` | med | S | `ratch/ingest/sources.py` is an untested fork of the maintained `service_kit.lakehouse.sources` | `packages/ratch/src/ratch/ingest/sources.py:29` |
| `ratch-011` | med | S | `Stage.client` declares the capability the composition root should bind, and the composition root ignores it | `packages/ratch/src/ratch/core/registry.py:73` |
| `CAT-CORE-14` | low | S | The project registry imports two private helpers across a module boundary | `services/catalog/src/catalog/services/projects.py:24` |
| `DUP-18` | low | S | medallion repeats the publish-trigger try/except at five sites | `services/medallion/src/medallion/services/ingest_trigger.py:117` |
| `DUP-19` | low | S | Three hand-rolled `storage_options` builders bypass the shared `lance_storage_options` | `packages/service-kit/src/service_kit/lakehouse/objectfs.py:21` |
| `DUP-20` | low | S | service-kit exports two different `register_middleware` functions under one name | `packages/service-kit/src/service_kit/middleware.py:60` |
| `DUP-21` | low | M | Seven outbound HTTP call sites build a fresh httpx client per call | `services/medallion/src/medallion/services/ray_submit.py:107` |
| `F-LIN-06` | low | S | prune_runs' batch size is duplicated as a Python constant and a literal baked into the Cypher — a change to one silently under-prunes | `services/lineage/src/lineage/services/repository.py:291` |
| `PS-20` | low | S | The error-payload string is copy-pasted eight times across `dashboard.py` | `packages/ray-kit/src/ray_kit/dashboard.py:170` |
| `PS-26` | low | S | `consume.py` imports the private `_Model` across a module boundary | `packages/lineage-kit/src/lineage_kit/consume.py:37` |
| `SK-11` | low | M | Duplicate column-lineage builders + a same-named 3-tuple `ColumnEdge`, and stale TRANSITIONAL markers pointing at a gate whose target… | `packages/service-kit/src/service_kit/openlineage.py:115` |
| `SKG-17` | low | S | S3Source re-implements its own listing-and-sort inline instead of calling the _listing helper it already has | `packages/service-kit/src/service_kit/lakehouse/sources.py:86` |
| `catalog-api-14` | low | M | user_state.py and policies.py ship near-identical handler triples, four and three times over | `services/catalog/src/catalog/api/v1/endpoints/user_state.py:183` |
| `ratch-017` | low | S | Three modules import a leading-underscore private symbol across a module boundary | `packages/ratch/src/ratch/core/driver.py:34` |

### E7 — Structure: layering, God modules, readability

**P2** · 37 issues (0 high, 17 medium, 20 low)

The user's original complaint, quantified: five modules over 700 lines carrying 6-12 unrelated concerns, orchestration living in endpoint bodies, and functions with 5-level nesting.

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `CAT-CORE-04` | med | L | `dataplane.py` is a 1438-line God module carrying twelve unrelated concerns, including HTTP protocol semantics | `services/catalog/src/catalog/services/dataplane.py:155` |
| `F-LIN-03` | med | L | repository.py is a 1382-line God module mixing a Cypher DSL, two storage surfaces, DDL bootstrap, cluster coordination and event synthesis | `services/lineage/src/lineage/services/repository.py:86` |
| `F-LIN-08` | med | M | Route topology is decided at import time by four settings-conditional module-level gates, none drivable from a test | `services/lineage/src/lineage/main.py:37` |
| `F-LIN-10` | med | M | The cron sweep is a 114-line function, and reconcile_all serially awaits three independent round-trips per dataset | `services/lineage/src/lineage/api/reconcile_cron.py:41` |
| `MAINT-09` | med | L | run_sweep (172 lines) and compact_one (153 lines) are god-functions with 5-level nesting mixing discovery, registry reads, policy… | `services/maintenance/src/maintenance/services/sweep.py:94` |
| `MED-004` | med | L | `handle_stage` is a 473-line God function that is the entire module — lane routing, tenant resolution, authz, compute dispatch, lineage,… | `services/medallion/src/medallion/services/transform.py:74` |
| `PS-10` | med | M | tracker's backend-agnostic contract is broken: the schema migration is SQLite-only, and DDL runs in the constructor with no migration path | `packages/tracker/src/tracker/_base.py:70` |
| `PS-19` | med | M | `cluster_status` is a 78-line function with a nested try inside a try inside a loop, doing five things | `packages/ray-kit/src/ray_kit/dashboard.py:275` |
| `SK-09` | med | M | Lifecycle invariants the shared probes depend on are unenforced convention, hand-rolled in every service | `packages/service-kit/src/service_kit/probes.py:56` |
| `X1` | med | M | DECISION ITEM: the estate runs three entrypoint families, two error taxonomies, three health conventions and two OTel wiring paths — the… | `packages/service-kit/src/service_kit/__init__.py:90` |
| `X12` | med | S | viewer/search test-seam factories build a different app than production — no problem handlers, no probes — so the exact regression their… | `services/viewer/src/viewer/main.py:115` |
| `X8` | med | M | The four `make_service_app` services expose liveness only, and the chart points BOTH k8s probes at it — no readiness, no drain signal,… | `services/compute/src/compute/health.py:16` |
| `catalog-api-03` | med | L | Multi-step orchestration lives in endpoint functions instead of services/, despite the service having a services/ layer | `services/catalog/src/catalog/api/v1/endpoints/data.py:112` |
| `catalog-api-05` | med | S | Eleven wire schemas are defined inline in endpoint modules although schemas.py declares itself the single home — the exact drift its… | `services/catalog/src/catalog/schemas.py:1` |
| `ingest-flow-10` | med | M | drain_chunk and finalize_run are oversized multi-purpose functions with nested closures and a coroutine redefined inside a loop | `services/ingest/src/ingest/worker.py:289` |
| `ingest-flow-12` | med | M | The scope is ~45-58% prose (52% overall, measured), and the prose is PR narrative — dated measurements, named test files, and a changelog… | `services/ingest/src/ingest/runtime.py:29` |
| `ratch-012` | med | L | Six functions run 65-120 lines doing several distinct jobs, two of them near-duplicates of each other | `packages/ratch/src/ratch/cli/speaker.py:177` |
| `ANN-17` | low | S | Throwaway class as a response stand-in, and a doubled `.json()` parse, in the publish transport | `services/annotator/src/annotator/projects/lakehouse.py:155` |
| `ANN-19` | low | S | Function-local stdlib imports beyond the cases the convention justifies | `services/annotator/src/annotator/api/v1/endpoints/project_events.py:499` |
| `ANN-20` | low | S | Member grant/revoke uses DELETE with a required request body | `services/annotator/src/annotator/api/v1/endpoints/members.py:136` |
| `CAT-CORE-17` | low | S | `create_table` is a 36-line pass-through that forwards every argument to `_create_table_direct` | `services/catalog/src/catalog/services/dataplane.py:155` |
| `COMPUTE-UNBOUNDED-QUERY-PARAMS` | low | S | `lines` is forwarded unbounded to the Ray dashboard; both it and `tail` are declared without `Annotated[..., Query(...)]` | `services/compute/src/compute/routes.py:35` |
| `F-LIN-14` | low | S | Only one of eight routers under api/v1/endpoints/ carries a version prefix, producing a double-/api path for ingest | `services/lineage/src/lineage/api/v1/endpoints/ingest.py:22` |
| `GW-ROUTE-TUPLE` | low | S | Route rows are positional 4-tuples read by index, and `_merged_openapi` returns a bare `dict` | `services/gateway/src/gateway/__init__.py:94` |
| `ING-18` | low | S | Query-parameter clamping done by hand instead of declared, and a frozen-model idiom used on a mutable model | `services/ingest/src/ingest/api.py:232` |
| `MAINT-13` | low | M | Routes are registered at import time from a module-level get_settings(), with tags on each route instead of the APIRouter | `services/maintenance/src/maintenance/api/routes.py:157` |
| `MAINT-16` | low | M | Helpers with 4-5 positional parameters and functions that mutate a caller-owned report/output argument in place | `services/maintenance/src/maintenance/services/sweep.py:54` |
| `MED-014` | low | S | Both app entrypoints read settings and configure logging at import time | `services/medallion/src/medallion/mover.py:36` |
| `SK-17` | low | M | No `__all__` and no public/private marking: importing anything from `service_kit` executes the app factory module | `packages/service-kit/src/service_kit/__init__.py:1` |
| `SK-22` | low | S | CORS is registered first and therefore ends up the INNERMOST of five middleware layers | `packages/service-kit/src/service_kit/middleware.py:7` |
| `VS-20` | low | S | `parse_range` returns a three-way `tuple \| str-sentinel \| None` that the caller decodes with a rebound flag variable | `services/viewer/src/viewer/api/v1/endpoints/media.py:78` |
| `VS-21` | low | S | Eleven route params use bare defaults instead of the `DatasetParam`/`Query(...)` aliases the same package defines | `services/viewer/src/viewer/api/v1/endpoints/graph.py:306` |
| `VS-22` | low | S | Store-name/bucket-name confusion makes the object browser's 404 name the wrong thing | `services/viewer/src/viewer/api/v1/endpoints/objects.py:159` |
| `X13` | low | S | The three media apps' lifespans have no try/finally around `yield`, unlike the five other lance apps | `services/viewer/src/viewer/main.py:78` |
| `catalog-api-19` | low | S | create_table runs a parent-existence round trip before the cheap request-shape validations it should follow | `services/catalog/src/catalog/api/v1/endpoints/data.py:151` |
| `ratch-015` | low | M | The Typer CLI prints through 54 bare `typer.echo` calls and hand-rolls column widths; `rich` is not a dependency | `packages/ratch/src/ratch/cli/pipeline.py:30` |
| `ratch-016` | low | S | `_run_in_process` passes arguments by mutating the global `sys.argv` | `packages/ratch/src/ratch/core/jobs.py:224` |

### E8 — Throughput: N+1 round trips, unbounded reads, per-call clients

**P2** · 37 issues (4 high, 27 medium, 6 low)

Sequential awaits over independent I/O, full-table reads to serve one row, and connection pools opened and destroyed per call. Several are super-linear in corpus size, so they fail at the scale the docs advertise.

#### The high-severity items in this epic

<details><summary><b>ANN-03</b> — Per-item actor round-trips are awaited sequentially on send, publish and list — up to 2000 serialised sidecar calls in one request <i>(annotator, resources, effort M)</i></summary>

**Sites:** `services/annotator/src/annotator/api/v1/endpoints/project_events.py:457`, `services/annotator/src/annotator/api/v1/endpoints/project_events.py:464`, `services/annotator/src/annotator/api/v1/endpoints/project_events.py:467`, `services/annotator/src/annotator/projects/saga.py:136`, `services/annotator/src/annotator/projects/saga.py:138` *(+3 more)*

**Why it matters.** HOUSE-RULE-15: independent I/O awaited sequentially. At the module's own 1000-task cap this is 2000 serialised cross-process Dapr calls inside one HTTP request; at even 20 ms each that is 40 s, past any gateway/ingress timeout. The recovery is not free: the module docstring at `:388-394` argues the seed→index order specifically to make a crash mid-send safe *because re-sending repairs it* — a timed-out send therefore requires a manual re-send, and the publish precondition reads a half-populated index until someone does. `collect()` has the same shape on the publish path, which the actor docstring (project_actor.py:8-11) explicitly says must "not be slow or flaky".

**Fix.** Apply the pattern already present at `project_events.py:507`: bound concurrency with `asyncio.Semaphore` and fan out with `asyncio.gather`/`TaskGroup`. Per-task the seed→send order must stay sequential, but *different* tasks are independent, so gather over items with the two calls chained inside one coroutine. Same for `saga.collect` (gather `get`+`get_draft` per task) and `projects.list_projects`.

**Verifier (CONFIRMED).** All four shapes verified. project_events.py:423-469 loops items and awaits `_task_proxy(...).seed(body)` (:464) then `_project_proxy(project_id).send(body)` (:467) — two serialised sidecar RPCs per task, with the `len(items) * consensus_n > 1000` cap at :413. saga.py:127-144 `collect` awaits `handle.get()` (:136) then `handle.get_draft()` (:138) per task id. projects.py:150-153 awaits…

</details>

<details><summary><b>VS-04</b> — Search result-cache key omits `spec.table`, so two different searchable tables of one corpus serve each other's hits <i>(viewer-search, resources, effort S)</i></summary>

**Sites:** `services/search/src/search/services/result_cache.py:71`, `services/search/src/search/services/result_cache.py:94`, `services/search/src/search/services/result_cache.py:38`, `services/search/src/search/api/v1/router.py:231`, `services/search/src/search/services/service.py:375`

**Why it matters.** The module's own docstring says "Miss one and a stale result is served, so the payload is exhaustive." One is missed. `GET /api/search?q=x&table=pages` then `GET /api/search?q=x&table=lines` against the same corpus produce identical cache keys, so the second request returns the first table's rows — with `_table` stamped as the first table, so the frontend renders them as if they were correct. Secondarily, `version_signature` only versions the default search's tables, so a write to a non-default searchable table never invalidates anything. `search_cache_size` defaults to 256 (media/config.py:47), i.e. this is on by default.

**Fix.** Add `"table": spec.table` to the `query_hash` payload, and make `_search_tables` resolve through `declared.search_named(spec.table)` so the version signature covers the tables actually read. Add a unit test asserting two specs differing only in `table` produce different keys — `tests/unit/test_search_table_selector.py` already exercises the selector and is the natural home.

**Verifier (CONFIRMED).** Verified end to end. result_cache.query_hash (71-91) payload lists mode/q/q_vec/n/where/rerank/rerank_n/weight/fuzziness/phrase/prefilter/filters/image — `spec.table` is absent, and `SearchSpec.table` genuinely exists (spec.py:91) and is bound as a GET query param via `spec: Annotated[SearchSpec, Query()]` (router.py:106). cache_key (100) = (handle.id, version_signature, query_hash), and…

</details>

<details><summary><b>VS-05</b> — `/api/page` and `/api/pages` materialize every page blob in the dataset to serve one image or one metadata page <i>(viewer-search, resources, effort M)</i></summary>

**Sites:** `services/viewer/src/viewer/api/v1/endpoints/pages.py:188`, `services/viewer/src/viewer/api/v1/endpoints/pages.py:189`, `services/viewer/src/viewer/api/v1/endpoints/pages.py:192`, `services/viewer/src/viewer/api/v1/endpoints/pages.py:156`, `services/viewer/src/viewer/api/v1/endpoints/pages.py:158`

**Why it matters.** The route's docstring correctly warns that inlining bytes into the listing "would move hundreds of megabytes to render a contact sheet" — and then the single-page route does exactly that per request. A contact sheet of 100 thumbnails issues 100 requests, each reading the entire volume: memory and S3 egress scale as O(pages²). This is the primary read path of the document viewer.

**Fix.** Push the predicate down: `read_aligned_table(ds, columns=[*_PAGE_COLUMNS, "payload"], filter=f"id = {page_id}")` for the single-page route, and read only `_PAGE_COLUMNS` plus a cheap null-probe (not the blob) with `limit=limit` for the listing — `has_image`/`size` can come from a blob-description read rather than the payload itself.

**Verifier (CONFIRMED).** Verified. pages.py:188 `rows = await run_in_threadpool(read_aligned_table, ds, columns=[*_PAGE_COLUMNS, "payload"])` with no predicate, then membership/index lookup at 189-192; list_pages does the identical full read at 156 and only slices afterwards at 158 (`range(min(rows.num_rows, limit))`). `payload` is the blob column, so both read every page's bytes. The docstring irony is real…

</details>

<details><summary><b>ingest-flow-03</b> — discover_staged's exact-cover search is super-linear in the run's whole unit universe and recurses once per fragment — it cannot finish the… <i>(ingest-flow, resources, effort L)</i></summary>

**Sites:** `services/ingest/src/ingest/staging.py:191`, `services/ingest/src/ingest/staging.py:192`, `services/ingest/src/ingest/staging.py:249`, `services/ingest/src/ingest/staging.py:258`, `services/ingest/src/ingest/runtime.py:331`

**Why it matters.** The module's own scale statement is a million-unit harvest (`workflow.py` docstring, `CHUNK_SIZE = 1000`). A 1M-unit run stages ~1000 manifests over a 1,000,000-key universe: the FIRST search node builds a million-entry dict, each doing ~1000 subset tests of ~1000 elements — that call does not return. Even the benign no-overlap case, which picks one fragment per node, recurses to depth ≈ number of fragments, so ~1000+ staged fragments hits Python's default recursion limit and raises `RecursionError` — inside `finalize_run`, the single commit point for the whole run, after every byte has already been fetched and staged. `_SEARCH_NODE_LIMIT` returning `None` is worse, not better: `discover_staged` translates it into `StagingOverlapError` ("no selection covers every unit exactly once"), so a run that had a perfectly clean cover is refused as if it were corrupt.

**Fix.** Index once instead of rescanning: build `unit -> [fragment index]` and a per-fragment remaining-count, then run the cover iteratively (explicit stack, no recursion). Short-circuit the overwhelmingly common case first — if the staged unit sets are pairwise disjoint, the cover is all of them, no search needed. Scope the cover per CHUNK rather than per run (staging roots are already per run; adding the chunk segment makes each search a handful of fragments by construction), and bound by unit count, not node count. Add a scale test with 1000+ manifests over 10^5 units asserting `discover_staged` returns in bounded time and does not raise.

**Verifier (CONFIRMED).** All five sites exact. `discover_staged` (staging.py:135) reads `staging_root(dataset_uri, run_id)` — per RUN, confirmed at :68-70, no chunk segment — then :191 `staged_units = frozenset().union(*(units for units, _ in records))` and :192 `chosen = _exact_cover([units for units, _ in records], staged_units)`. staging.py:258 is verbatim the per-node dict comprehension over all of `remaining`, with…

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `ANN-08` | med | M | Version history opens and scans one Lance snapshot per version, up to 200 per request | `services/annotator/src/annotator/annotations/versions.py:89` |
| `ANN-09` | med | S | `project_facet` rescans every published row once per distinct task — quadratic on the publish path | `services/annotator/src/annotator/projects/publish.py:352` |
| `CAT-CORE-09` | med | M | Each mutating table op performs three full namespace describes plus three dataset opens | `services/catalog/src/catalog/services/dataplane.py:94` |
| `CAT-CORE-10` | med | S | The commit path issues unbounded serial object-store round trips — one HEAD per data file, one transaction read per version | `services/catalog/src/catalog/services/dataplane.py:702` |
| `CP-INGRESS-N-PLUS-ONE` | med | S | One blocking Kubernetes API call per project to resolve ingress hosts, where one cluster-wide call would do | `services/controlplane/src/controlplane/service.py:42` |
| `F-LIN-04` | med | M | Every list/browse read is an unbounded fetch-all with no server-side LIMIT, on endpoints documented as polled every 2s | `services/lineage/src/lineage/services/repository.py:111` |
| `GW-BUFFERS-REQUEST-BODY` | med | M | Every proxied request body is fully buffered in memory, though responses are correctly streamed | `services/gateway/src/gateway/__init__.py:327` |
| `GW-OPENAPI-SEQUENTIAL` | med | S | `/openapi.json` fetches ten backends sequentially with a 10 s timeout each — worst case ~100 s on a client-facing request | `services/gateway/src/gateway/__init__.py:238` |
| `ING-08` | med | M | A fresh HTTP client is built for every outbound call — including one per fetched unit, the exact defect this service's own docstring warns… | `services/ingest/src/ingest/fetch.py:83` |
| `MAINT-04` | med | M | COMPLETE lineage emits are awaited one-per-dataset inside the loop and uncapped, while the FAIL emits next to them are gathered AND capped… | `services/maintenance/src/maintenance/services/sweep.py:317` |
| `MAINT-12` | med | M | The multi-base layout gate issues one sequential S3 HEAD per referenced path with no batching and no short-circuit, on every dataset in… | `services/maintenance/src/maintenance/services/orphans.py:283` |
| `MED-008` | med | M | Every outbound HTTP call builds its own httpx client — one connection pool opened and torn down per Ray submit, per catalog register, and… | `services/medallion/src/medallion/services/ray_submit.py:107` |
| `PS-16` | med | S | Independent dashboard GETs are awaited sequentially in three functions | `packages/ray-kit/src/ray_kit/dashboard.py:281` |
| `SK-03` | med | M | A fresh urllib3 `ApiClient` is constructed per catalog read/write call and never closed | `packages/service-kit/src/service_kit/lancekit/reader.py:348` |
| `SK-06` | med | M | The dataset registry is lazily built inside request handling, unguarded, and the handles are never released | `packages/service-kit/src/service_kit/media/state.py:78` |
| `SK-12` | med | M | `lancekit.store` builds a new `pyarrow.fs.S3FileSystem` per call and bypasses `packages/storage` | `packages/service-kit/src/service_kit/lancekit/store.py:27` |
| `SKG-07` | med | S | make_client returns an aiohttp-backed OpenFgaClient with no disposal contract, and seven of its nine fleet call sites never close it (only… | `packages/service-kit/src/service_kit/governed/fga.py:318` |
| `SKG-11` | med | M | Module-level mutable cache global in warehouse_registry with no bound and no eviction | `packages/service-kit/src/service_kit/lakehouse/warehouse_registry.py:49` |
| `SKG-12` | med | S | The JWKS fetch has no explicit timeout while the discovery fetch beside it does | `packages/service-kit/src/service_kit/governed/oidc.py:207` |
| `VS-08` | med | M | The Cypher engine cache is a module-level mutable global holding hundreds of MB, and its lock serializes every graph request across all… | `services/viewer/src/viewer/api/v1/endpoints/graph.py:191` |
| `VS-12` | med | S | `_resolve` builds a new `httpx.Client` per catalog call while a pooled client sits on `app.state` | `services/viewer/src/viewer/api/v1/endpoints/pages.py:84` |
| `VS-15` | med | M | `download_object` buffers whole objects in memory on a premise the store registry no longer guarantees | `services/viewer/src/viewer/api/v1/endpoints/objects.py:323` |
| `VS-16` | med | M | Voice similarity issues one Lance scan per hit (N+1) and a fresh ThreadPoolExecutor per encoder call | `services/viewer/src/viewer/services/voice_service.py:423` |
| `catalog-api-11` | med | M | Cascade and enumeration paths await independent object-store I/O one item at a time | `services/catalog/src/catalog/api/v1/endpoints/tables.py:148` |
| `ingest-flow-13` | med | S | A boto3 client is constructed per staging call (twice per read/purge), and the S3 response bodies are never closed | `services/ingest/src/ingest/staging.py:310` |
| `ingest-flow-15` | med | S | InMemoryRunStore grows without bound and re-sorts the whole map on every recent() call | `services/ingest/src/ingest/runs.py:100` |
| `ratch-005` | med | S | Every vLLM client opens an httpx connection pool that nothing ever closes | `packages/ratch/src/ratch/clients/base.py:34` |
| `ANN-11` | low | M | Actor documents grow without bound and are fully re-serialized on every event | `services/annotator/src/annotator/projects/actor.py:171` |
| `F-LIN-09` | low | S | The AGE pool leaks if any of the seven lifespan bootstrap steps fails after pool.open() | `services/lineage/src/lineage/main.py:62` |
| `F-LIN-15` | low | S | Module-level mutable caches in the demo endpoint, with a test-only reset seam instead of app.state | `services/lineage/src/lineage/api/v1/endpoints/demo.py:75` |
| `ING-11` | low | S | The OpenFGA client is built on app.state and never closed — the lifespan's cleanup block disposes only the workflow runtime | `services/ingest/src/ingest/__init__.py:85` |
| `MAINT-03` | low | S | reconcile's load_sources awaits six independent stores sequentially where one asyncio.gather would do | `services/maintenance/src/maintenance/services/reconcile.py:511` |
| `PS-11` | low | S | tracker owns an Engine + a long-lived Session but is not a context manager | `packages/tracker/src/tracker/_base.py:71` |

### E9 — Tests that exist but never run, and code with none at all

**P1** · 9 issues (4 high, 4 medium, 1 low)

Two service suites are on disk and outside `testpaths` — including the two that pin a privilege-escalation and a commit-duplication regression. Three more packages/services have no tests at all.

> **RE-MEASURED 2026-08-22, migrated in from `open_test_audit.md` (H17/H20/H21).** The two enrolment
> items LANDED — `services/catalog/tests` and `services/lineage/tests` are both in `testpaths` as of
> 2026-08-09. What remains is the harder half, and it grew:
>
> | | state | scale |
> | --- | --- | --- |
> | `services/search` | no `tests/` at all, absent from `testpaths` | 2,614 tracked LOC |
> | `services/viewer` | no *committed* `tests/`, absent from `testpaths` | 4,288 |
> | `packages/ratch` | no `tests/` at all, absent from `testpaths` | 7,602 |
> | | | **14,504 lines** |
>
> `services/search` is half of what CLAUDE.md says rask IS ("ANNOTATE and SEARCH the data"). Its
> `frames.py:41 _ranked_or_fallback` runs at 16–33% line coverage and its shape is
> `try: return rank(scoped=True) / except: pass` → fall through, so **a search plane that has stopped
> ranking anything returns an empty 200 no test can tell from "no hits"**.
>
> **The sealed runners (H20).** Seven of nine (`asr`, `assist`, `diarize`, `insid3`, `kg`, `topics`,
> `voiceprint`) ship no tests. The 75 test functions that DO exist (56 `htr` + 19 `dummy`) run in **no
> CI job** — `dagger call test` covers the root testpaths only and says so in its own doc comment;
> `make test`/`make test-slow` name the runners by hand. *Not* a finding: those seven carrying no
> `uv.lock`. Per `.claude/skills/rask-architecture`, a runner carries a lock only where it builds an
> image (`assist`, `dummy`, `htr`) — the offline Ray Data runners let Ray install the env via
> `runtime_env`. That sub-claim is REFUTED.
>
> **The live e2e lanes (H17).** CI executes 12 of the 26 `tests/e2e-py` suites — the 12 named as PATHS
> by `scripts/e2e_stack.sh`, `scripts/ray_e2e_stack.sh` and `.dagger/e2e.go`. No `run:` line in
> `ci.yml` names any of the 13 per-suite markers, so 14 suites (46 of the 88 live assertions —
> including the medallion cascade proof, the governed-union authz proof and the registry-CAS proof)
> run in no automated lane. The 13 `make e2e-<suite>` targets exist but pytest exits 0 when every
> selected test skips, so each reports success while executing nothing.
>
> The enrolment MECHANISM (why globbed membership and explicit `testpaths` drift apart) is written into
> `.claude/skills/rask-architecture` § Hard invariants, so it survives this file being drained.

#### The high-severity items in this epic

<details><summary><b>CAT-CORE-03</b> — The catalog's in-service test directory is not in `testpaths`, so the commit-idempotency tests never run in CI <i>(catalog-core, testing, effort S)</i></summary>

**Sites:** `pyproject.toml:188`, `services/catalog/tests/test_commit_idempotency.py:1`

**Why it matters.** House rule 1: testpaths are EXPLICIT, so a test directory that is not listed is invisible. These five tests are the only automated protection on the replay guard whose failure mode the same file documents as "nine copies per file" — the tests are green and simply never asked to run. Any regression in `_find_run_commit` or `commit_appended_fragments` ships silently.

**Fix.** Add `"services/catalog/tests"` to `testpaths` in the root `pyproject.toml`. While there, audit the remaining `services/*/tests` directories for the same omission — the listed set covers five services and the estate has more.

**Verifier (CONFIRMED).** Verified empirically. pyproject.toml:188-189 lists packages/*/tests plus services/gateway|ingest|compute|controlplane|flows/tests, tests/unit, tests/integration, tests/e2e-py — services/catalog/tests is absent. `uv run pytest --collect-only -q` collects 3160 tests and `grep -c 'catalog/tests'` over the collection returns 0. services/catalog/tests/ contains exactly test_commit_idempotency.py, and…

</details>

<details><summary><b>F-LIN-01</b> — services/lineage/tests is absent from root testpaths — 18 tests, including two security regressions, never run <i>(lineage, testing, effort S)</i></summary>

**Sites:** `pyproject.toml:188`, `services/lineage/tests/test_privileged_identity.py:45`, `services/lineage/tests/test_external_source_authz.py:118`

**Why it matters.** Pytest discovery is explicit here (HOUSE-RULE-1), so a suite outside testpaths is silently inert. These two files were written as regression pins for a measured privilege-escalation (shared app token claiming `service-trainer`, `writer` on `namespace:models`) and a measured 403-on-every-honest-producer bug. A refactor of `_service_principal` or `is_external_source` that reintroduces either defect goes green in CI. That is worse than having no tests: the files' existence asserts a protection the gate does not actually enforce.

**Fix.** Add `"services/lineage/tests"` to `testpaths` in the root `pyproject.toml`, then run the suite and reconcile the `@pytest.mark.anyio` markers with the house `@pytest.mark.asyncio` convention. Audit the other lance-plane services for the same omission — `services/catalog`, `medallion`, `maintenance`, `viewer`, `search`, `annotator` are equally absent from that list.

**Verifier (CONFIRMED).** Verified at HEAD. pyproject.toml:188-189 lists 13 testpaths entries; `services/lineage/tests` is not among them (only `services/gateway/tests`, `ingest`, `compute`, `controlplane`, `flows` from services/). `uv run pytest services/lineage/tests -q` collects and passes 18 tests in 1.34s, so the suite is real and green but inert under the gate. Both named regressions exist verbatim:…

</details>

<details><summary><b>X2</b> — `services/catalog/tests` and `services/lineage/tests` are not in `testpaths` — 18 tests guarding a commit-replay duplication bug and a… <i>(cross-service, testing, effort S)</i></summary>

**Sites:** `pyproject.toml:188`, `services/catalog/tests/test_commit_idempotency.py:1`, `services/lineage/tests/test_privileged_identity.py:1`, `services/lineage/tests/test_external_source_authz.py:1`

**Why it matters.** `make test` / `make test-slow` pass no path arguments, so pytest uses `testpaths` — these three files are inert. Two of them pin security properties (a shared app-token could otherwise claim a privileged service identity) and one pins a data-duplication property on the ingest→catalog commit door. A regression in any of them ships green.

**Fix.** Add `"services/catalog/tests", "services/lineage/tests"` to `[tool.pytest.ini_options] testpaths` and run them once to confirm they still pass (test_commit_idempotency drives a real local Lance dataset, so check it is not slow-marked-worthy). Then add an invariant test — the estate already has `tests/unit/test_e2e_collection_gate.py` as the pattern — asserting that every `services/*/tests` and `packages/*/tests` directory on disk appears in `testpaths`, so the next service cannot repeat this.

</details>

<details><summary><b>ratch-001</b> — The whole package has zero tests and is not even wired into pytest's testpaths <i>(ratch, testing, effort L)</i></summary>

**Sites:** `packages/ratch/pyproject.toml:1`, `pyproject.toml:188`, `packages/ratch/src/ratch/core/__init__.py:5`, `packages/ratch/src/ratch/features/columns.py:5`, `packages/ratch/src/ratch/features/embed_columns.py:1` *(+1 more)*

**Why it matters.** 7,582 lines across 55 files, including the one sanctioned package CLI and all the Lance write paths (`core/dataset.py` is described as the single seam that makes create-time invariants unforgettable), carry no executable guarantee at all. Because the path is missing from `testpaths` AND pytest runs with explicit paths (`--import-mode=importlib`, no discovery), a test added under `packages/ratch/tests/` would silently never run — the gate is not merely empty, it is disconnected. The `Protocol` seams (`EmbeddingClient`, `CaptionClient`, `SummarizeClient`) and the zero-arg `factory` parameters threaded through `core/driver.py` exist specifically to permit offline fakes, so the design cost was already paid; only the tests are absent. The four docstrings asserting coverage actively mislead a reader into trusting behaviour nothing verifies.

**Fix.** Add `packages/ratch/tests` to `testpaths` in the root `pyproject.toml` in the same commit as the first test, so the wiring can never drift from the intent again. Start with the pure functions that need no Lance fixture (`retrieval/search.py`'s `extract_query_terms`/`timecode`/`parse_alignments_json`, `modalities/av/frames._jpeg_dimensions`, `features/topic_tree._nest`/`_subtree`, `lineage.column_map`), then the injectable seams the docstrings already promise: drive `features/embed_columns.embed_text_column` with a fake `EmbeddingClient` over a tmp_path Lance table, and `core/driver`'s resume paths (`_ValueCheckpoint` round-trip, the `take_blobs`-short-payload guard at `driver.py:109`) with…

**Verifier (CONFIRMED).** Verified end-to-end. `packages/ratch/tests` does not exist; root pyproject.toml testpaths (lines 188-189) lists 14 suites and none is under packages/ratch; `grep -rn 'from ratch\.' tests/ services/ packages/` outside ratch returns nothing (only runners/* import ratch, and runners are excluded from root pytest). Makefile:34-41 runs `uv run pytest` plus runners/htr and runners/dummy separately —…

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `PS-15` | med | S | `ray_kit.submit` — deterministic ids and the reattach-or-resubmit branch — has no tests, despite its own docstring naming it the hard part | `packages/ray-kit/src/ray_kit/submit.py:86` |
| `SKG-14` | med | M | The entire audited scope sits under a blanket 21-rule ruff exemption declared temporary for verbatim-copied files that have since been… | `pyproject.toml:109` |
| `VS-17` | med | L | Neither service is in `testpaths` and neither ships tests; the retrieval core and every pure helper are uncovered | `pyproject.toml:188` |
| `catalog-api-13` | med | S | services/catalog/tests is missing from the explicit pytest testpaths, so five tests never run | `pyproject.toml:188` |
| `TEST-CONFTEST-ENV-LEAK` | low | S | `compute/tests/conftest.py` mutates `os.environ` at import, leaking into every other suite in the session | `services/compute/tests/conftest.py:12` |

### E10 — Observability that is wired but does not emit

**P2** · 9 issues (1 high, 3 medium, 5 low)

The whole fleet ships zero logs to the collector, one service's INFO tier is dropped by a stale allow-list, and trace context does not cross the queue boundary.

#### The high-severity items in this epic

<details><summary><b>X4</b> — `configure_app_logging`'s allow-list is stale: `maintenance` calls it but is not on the list, while dead names `compaction` and `common` are <i>(cross-service, observability, effort S)</i></summary>

**Sites:** `packages/service-kit/src/service_kit/obs.py:27`, `services/maintenance/src/maintenance/service.py:45`, `services/maintenance/src/maintenance/api/routes.py:68`, `services/maintenance/src/maintenance/api/routes.py:125`, `services/maintenance/src/maintenance/services/optimize.py:199`

**Why it matters.** The whole purpose of this function, per its docstring, is that the OTel SDK's root LoggingHandler never fires for INFO because nothing raises the app level from WARNING. So the maintenance service's entire INFO tier is dropped: `maintenance_sweep` (routes.py:68), `reconcile_clean` (routes.py:125), `optimize_indices_disabled_by_policy`, `cleanup_disabled_by_policy`, the lance-trace bridge lines. Those are exactly the records an operator needs to answer 'did the sweep run and what did it compact' — the service is the estate's only destructive background job and it is observably silent.

**Fix.** Replace the hand-maintained tuple with the real set: add `maintenance`, `ingest`, `flows`, `compute`, `controlplane`, `gateway`; drop `compaction` and `common`. Better: derive it — the caller already knows its own package, so make the signature `configure_app_logging(*packages: str)` defaulting to the caller's top-level package via `__name__.split('.')[0]`, which cannot drift when a service is renamed. Add a unit test asserting each entrypoint module's root package logger is at INFO after import.

</details>

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `PS-23` | med | M | Dropped lineage events have no signal but a log line — and the authoring step sits inside the transport try/except | `packages/lineage-kit/src/lineage_kit/emitter.py:59` |
| `SK-07` | med | S | `configure_app_logging`'s logger allow-list has drifted from the fleet — `maintenance` opts in and gets nothing | `packages/service-kit/src/service_kit/obs.py:25` |
| `X5` | med | M | The fleet's own logging setup targets logger names that no longer exist, and `setup_otel` exports no logs at all despite claiming to | `packages/service-kit/src/service_kit/__init__.py:31` |
| `CAT-CORE-18` | low | S | The OpenLineage `producer` URI stamped on every emitted event points at the wrong repository and a non-existent path | `services/catalog/src/catalog/core/lineage_emit.py:107` |
| `GW-OPENAPI-SILENT-SHADOW` | low | S | The merged OpenAPI silently drops colliding paths — every service's `/api/health` overwrites the previous one | `services/gateway/src/gateway/__init__.py:248` |
| `MAINT-15` | low | S | Identity drift left over from the compaction→maintenance rename: a wire-visible OpenLineage producer URI pointing at a path and repo that… | `services/maintenance/src/maintenance/core/lineage_emit.py:53` |
| `MED-016` | low | S | `record_quality_blocked` is incremented for an undecodable-media failure that ran no quality assertion | `services/medallion/src/medallion/services/transform.py:430` |
| `SK-16` | low | S | Lineage emitter logs under a hardcoded `"lineage"` logger and writes events straight to stdout | `packages/service-kit/src/service_kit/lancekit/lineage_emit.py:27` |

### E11 — Typing and public API surface

**P2** · 22 issues (0 high, 6 medium, 16 low)

Boundaries carried as `dict[str, Any]`, routes returning bare dicts (no response filtering, no OpenAPI contract), legacy `TypeVar`, and no `py.typed` on any package.

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `ANN-07` | med | M | Half the routes return bare `dict[str, Any]` — raw actor documents reach clients with no schema or filtering | `services/annotator/src/annotator/api/v1/endpoints/tasks.py:165` |
| `CAT-CORE-08` | med | M | Service functions return `dict[str, Any]` that endpoints splat into Pydantic models, so the boundary is unchecked | `services/catalog/src/catalog/services/maintenance.py:64` |
| `CAT-CORE-12` | med | M | Three object-store registries parse JSON into raw dicts and hand-validate with `.get()` truthiness checks | `services/catalog/src/catalog/services/warehouses.py:126` |
| `F-LIN-07` | med | M | Domain values cross the models→repository boundary as untyped dicts and positional tuples while typed Pydantic twins exist on the read path | `services/lineage/src/lineage/models.py:68` |
| `SKG-09` | med | L | Every lakehouse control-plane record is an unvalidated dict[str, Any]; write paths index required keys with no boundary validation | `packages/service-kit/src/service_kit/lakehouse/protection.py:46` |
| `VS-18` | med | M | Ten routes return bare `dict[str, Any]` / `list[dict[str, Any]]`, losing response filtering and the OpenAPI contract | `services/viewer/src/viewer/api/v1/endpoints/atlas.py:81` |
| `ANN-15` | low | S | `@dataclass` on a value object where the house rule is Pydantic | `services/annotator/src/annotator/projects/saga.py:38` |
| `ANN-16` | low | S | `Any` used for collaborators that already have a declared Protocol | `services/annotator/src/annotator/api/v1/endpoints/project_events.py:158` |
| `CAT-CORE-15` | low | S | Maintenance ops type their dataset parameter as `Any` where a small Protocol would state the contract (style, not a gate violation) | `services/catalog/src/catalog/services/maintenance.py:21` |
| `CAT-CORE-16` | low | S | `@dataclass` value object and a duplicated legacy type alias in the data plane | `services/catalog/src/catalog/services/dataplane.py:724` |
| `CP-CR-UNVALIDATED` | low | M | Kubernetes CRs are walked as `dict[str, Any]` with `.get()` chains instead of validated at the boundary | `services/controlplane/src/controlplane/service.py:24` |
| `F-LIN-16` | low | S | Generic return widened to Any internally, bare list defaults on two Pydantic fields, and unannotated conn params | `services/lineage/src/lineage/api/fga_deps.py:246` |
| `ING-15` | low | M | The source registry stores its callables as `object` and casts them back with three `# type: ignore[assignment]`; four public functions… | `services/ingest/src/ingest/sources.py:100` |
| `MED-013` | low | S | Two handler seams are typed `Any` with an ANN401 suppression while every sibling handler is concretely typed | `services/medallion/src/medallion/services/publication_trigger.py:66` |
| `PS-12` | low | S | `TrackerProtocol` omits `flush()`, which is in every backend's documented usage | `packages/tracker/src/tracker/protocol.py:11` |
| `PS-14` | low | S | `@dataclass` used for a value object (Pydantic-only estate) | `packages/validate/src/validate/rules.py:12` |
| `PS-27` | low | S | None of the five packages ships a `py.typed` marker | `packages/storage/pyproject.toml:22` |
| `SK-19` | low | S | `dataset_handle` (the shared media resolution entry point, ~34 callers) has no return annotation — inside an explicitly ANN-exempt tree | `packages/service-kit/src/service_kit/media/state.py:68` |
| `SKG-16` | low | S | Any/object on public signatures where a Protocol or Callable alias exists, and a decorator factory with no return annotation | `packages/service-kit/src/service_kit/governed/deps.py:66` |
| `VS-23` | low | S | Legacy `TypeVar` instead of PEP 695 type parameters, and a shadowed function parameter | `services/search/src/search/services/encoders/base.py:25` |
| `catalog-api-15` | low | S | Annotated-Depends aliases are used as plain parameter annotations on functions that are not dependencies | `services/catalog/src/catalog/api/v1/endpoints/access.py:87` |
| `ratch-018` | low | S | Legacy `TypeVar` generics in the shared HTTP transport instead of PEP 695 | `packages/ratch/src/ratch/clients/base.py:13` |

### E12 — Delete what nothing calls

**P3** · 14 issues (0 high, 4 medium, 10 low)

Whole modules, packages and settings with zero callers — including one that re-introduces the relational store P7a removed, and one 217-line module added but never wired.

#### The rest of the epic

| ID | Sev | Effort | Issue | Primary site |
| --- | --- | --- | --- | --- |
| `MAINT-11` | med | S | core/lance_trace.py is a 217-line module with zero callers and zero tests, plus an unused constant in orphans.py | `services/maintenance/src/maintenance/core/lance_trace.py:183` |
| `PS-09` | med | S | `packages/tracker` has zero consumers and reintroduces the relational store P7a removed — while pulling sqlmodel + psycopg into the root… | `packages/tracker/src/tracker/__init__.py:1` |
| `PS-13` | med | S | `validate/rules.py` is entirely unconsumed — 5 exported symbols, 0 callers, 0 tests | `packages/validate/src/validate/rules.py:20` |
| `ratch-008` | med | S | Two whole modules and four helpers/settings with no caller anywhere in the repo | `packages/ratch/src/ratch/lineage.py:1` |
| `ANN-18` | low | S | Shutdown closes resource slots the annotator never populates | `services/annotator/src/annotator/main.py:104` |
| `F-LIN-12` | low | S | An unused module constant, and a 45-line 'consumer side' of the run hierarchy that no production code reads | `services/lineage/src/lineage/main.py:38` |
| `ING-16` | low | S | Three dead entry points, two of them carrying docstrings that assert they are load-bearing | `services/ingest/src/ingest/lineage.py:69` |
| `MED-015` | low | S | Unreachable return, a stale line reference, a truncated docstring — plus one inline comment (not a docstring) whose open-count claim is… | `services/medallion/src/medallion/services/ray_submit.py:199` |
| `PS-08` | low | S | `python-dotenv` is a declared dependency of `packages/storage` and is never imported | `packages/storage/pyproject.toml:10` |
| `PS-22` | low | S | Orphaned `#:` doc-comment in `submit.py` documents a constant that no longer exists | `packages/ray-kit/src/ray_kit/submit.py:45` |
| `SK-15` | low | S | `_setup_logging` configures logger trees no package in the repo produces — `RASK_LOG_LEVEL` is inert | `packages/service-kit/src/service_kit/__init__.py:31` |
| `SK-21` | low | S | Stale references to deleted packages, migrated gates and a renamed frontend layout | `packages/service-kit/src/service_kit/openlineage.py:17` |
| `catalog-api-20` | low | S | Two task-listing routes declare a CurrentToken dependency their bodies never use | `services/catalog/src/catalog/api/v1/endpoints/tables.py:479` |
| `ingest-flow-18` | low | S | Unreferenced helper and a legacy field still written on every manifest | `services/ingest/src/ingest/lander.py:235` |

---

## E13 — the toolchain rule the repository calls non-negotiable is enforced by a ratchet, not yet by zero

*Added 2026-08-22 from the test audit (its finding N1), which found this while fixing M12 and migrated
it here rather than patching it, because converting the sites is a scope decision.*

`CLAUDE.md` states the docker prohibition three times, escalating each time, and records it being
violated once: the build-only scoping "was read (2026-08-15) as licence to `docker run` a throwaway
NATS for a test repro." Until 2026-08-22 **nothing gated it** — while the frontend plane has had
`toolchain.test.ts` failing the build if ESLint or Prettier reappear the whole time.

**Already done, so this epic starts from a ratchet rather than from zero:** `tests/unit/test_no_docker.py`
now gates both tiers. Tier 1 (docker BUILDS an image) is absolute and passes with an EMPTY exemption
list — the estate's hardest clause holds today. Tier 2 (docker CREATES a container) is a shrink-only
roster: three known sites, two justified bootstrap exemptions.

The two exemptions are permanent and correct: `scripts/dagger-engine.sh` cannot use Dagger to create
the Dagger engine (a circular dependency, not a violation), and `scripts/k3s-registry.sh` creates the
registry Dagger pushes to, which must exist before a push can reach it.

**The work: retire the three roster entries.**

| site | what it starts | why it is a scope decision, not a patch |
| --- | --- | --- |
| `Makefile:161` `notifications-rig-up` | Mailpit + a counting Slack sink, from `.docker/docker-compose.notifications-channels.yml` | two services plus an inline Python webhook-sink script; converting means retiring the compose file, and `dagger core … as-service up` runs in the FOREGROUND where `docker compose up -d` detaches |
| `Makefile:463` `rustfs-up` | a local rustfs S3 server for the storage smoke | same foreground/detached UX change; `rustfs-down` disappears with it |
| `.github/workflows/ci.yml:436` | the per-zone image smoke test | mechanically the easiest, but that file is contended and the smoke needs the image already loaded into a daemon |

The pattern CLAUDE.md prescribes, and no module is needed for an ad-hoc service:

    dagger core container from --address=<img> with-exposed-port --port=<p> \
      with-default-args --args=<cmd> as-service up --ports=<host>:<p>

**The UX change is the whole decision, and it should be made deliberately.** Every one of these is a
detached dev convenience today (`up -d`, then the developer keeps working). The Dagger equivalent holds
a terminal. That is not a reason to keep docker — the rule is not negotiable — but it IS a reason the
conversion needs an owner's call on the resulting loop, rather than a silent swap that makes three
familiar targets behave differently one morning.

## E14 — `build_settings()` mutates the process environment permanently, and its isolation fixture defers to the ambient one

*Added 2026-08-22 from the test audit (L4), which fixed the consequence and migrated the cause.*

`packages/service-kit/src/service_kit/__init__.py`:

    def build_settings() -> Settings:
        load_dotenv()
        derive_hcp_creds()
        return Settings.model_validate({})

`derive_hcp_creds()` writes derived credentials into `os.environ` — permanently, unrestorably, for the
whole process. Every app built after it inherits them, including apps built by later tests in the same
session. The one fixture that claims to isolate this defers to whatever the ambient environment already
says, so it cannot restore a value it never captured.

**Already done:** the test audit closed the consequence it could reach. All four Dagger build contexts
now exclude `**/.env`, so a local `dagger call` no longer ships a developer's untracked `.env` into the
container while CI, checking out fresh, cannot (measured: 2 entries before, 0 after, gated by
`tests/unit/test_dagger_context_is_hermetic.py`). That removes the divergence between a local run and
CI. It does NOT fix the seam.

**The work.** `load_dotenv()` at app-build time is load-bearing for local development and is not the
problem on its own; writing derived values into `os.environ` is. Options, in increasing order of change:

1. Have `derive_hcp_creds()` RETURN the derived values and let `Settings` take them, so nothing global
   is written. Smallest diff, but every caller reading them off `os.environ` has to be found first.
2. Scope the mutation to a context manager that restores on exit, so a test can build an app without
   leaking into the next one.
3. Move the derivation into `Settings` itself as a validator, which is where the estate puts
   "compute a field from other fields" everywhere else.

Whichever is chosen, the fixture has to capture-and-restore rather than assert-the-ambient — that half
is a bug regardless of which option wins.

## Appendix A — every finding, by scope

The table below is the complete, verified list. `→` names the epic each finding was filed under.
Nothing in the audit is omitted here; the epics above are a view over this table.

### `catalog-api`

*Layout as it actually is:* `services/catalog` follows sanctioned layout (b) — the fastapi-template shape — but only partially. `main.py` builds its own `FastAPI(...)` (no `make_service_app`), wires an `@asynccontextmanager` lifespan that constructs every client (native `LanceNamespace`, OpenFGA, optional `httpx.AsyncClient` + `DaprClient`, the credential vendor, the control ring buffer, the Dapr `UserStateStore`) onto `app.state` and disposes them in a `finally` with per-resource `suppress(Exception)`; error wiring is `service_kit.lakehouse.ns_errors.install_problem_handlers`, which registers handlers ONLY for `LanceNamespaceError`, `RequestValidationError` and a catch-all `Exception`. Config is `pydantic-settings` via `@lru_cache get_settings()` in `core/config.py`, injected as `SettingsDep`. Middleware: a decorator-style `maintenance_middleware` plus two pure-ASGI classes (`BodySizeLimitMiddleware`, `WriteConcurrencyLimitMiddleware`). DI is `Annotated[T, Depends(...)]` aliases in `api/dependencies.py` (`NamespaceDep`, `SettingsDep`, `StorageOptionsDep`, `FgaClientDep`, `LineageEmitterDep`, `ControlEmitterDep`, `VendorDep`) — no default-arg `Depends` anywhere, no legacy typing, no Pydantic-v1 constructs, no `requests`/`boto3`, and 163 `run_in_threadpool` calls keep blocking pylance/S3 work off the loop (sync `def` routes are used correctly for the purely-blocking reads). `api/v1/router.py` mounts 23…

*Tests:* Coverage is real but structurally split and has one dead pocket. The bulk lives in the root `tests/unit` (160 files) and exercises this scope indirectly through `httpx.ASGITransport` against the app: `test_access_admin.py`, `test_access_grant.py`, `test_projects_endpoint.py`, `test_project_delete.py`, `test_warehouse_delete.py`, `test_warehouse_namespaces_read.py`, `test_warehouses.py`, `test_binding_cache_eviction.py`, `test_body_limit.py`, `test_me_endpoint.py`, `test_user_state.py`, `test_control_events.py`, `test_client_direct_commit.py`, `test_fga_model_contract.py` (proves every `(type, relation)` `fga_deps` can check exists in the compiled model), `test_openapi_contract.py`.…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `catalog-api-01` | E4 | **HIGH** | error-handling | S | endpoints/stores.py raises service_kit.exceptions, which bypass the catalog's RFC 9457 problem handler entirely | `services/catalog/src/catalog/api/v1/endpoints/stores.py:30`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:59` *(+4 more)* |
| `catalog-api-02` | E1 | **HIGH** | security | S | GET /v1/stores and /v1/stores/tiers disclose the whole estate's buckets and hosts with no authorization gate, while the sibling POST calls that same set… | `services/catalog/src/catalog/api/v1/endpoints/stores.py:63`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:116` *(+3 more)* |
| `catalog-api-03` | E7 | med | structure | L | Multi-step orchestration lives in endpoint functions instead of services/, despite the service having a services/ layer | `services/catalog/src/catalog/api/v1/endpoints/data.py:112`, `services/catalog/src/catalog/api/v1/endpoints/warehouses.py:576` *(+8 more)* |
| `catalog-api-04` | E6 | med | coupling | S | Endpoint modules import each other's private helpers, and one imports a private helper out of api/dependencies | `services/catalog/src/catalog/api/v1/endpoints/tables.py:50`, `services/catalog/src/catalog/api/v1/endpoints/stores.py:29` *(+3 more)* |
| `catalog-api-05` | E7 | med | structure | S | Eleven wire schemas are defined inline in endpoint modules although schemas.py declares itself the single home — the exact drift its docstring cites a bug for | `services/catalog/src/catalog/schemas.py:1`, `services/catalog/src/catalog/api/v1/endpoints/warehouses.py:246` *(+9 more)* |
| `catalog-api-09` | E6 | med | duplication | M | The estate-gate / audit-on-outage preamble is hand-copied across eleven handlers and reaches into app.state.fga instead of using the existing FgaClientDep | `services/catalog/src/catalog/api/v1/endpoints/access.py:90`, `services/catalog/src/catalog/api/v1/endpoints/access.py:143` *(+9 more)* |
| `catalog-api-10` | E4 | med | error-handling | S | Two sibling access surfaces map the same client error to different spec codes — 501 UnsupportedOperation vs 400 InvalidInput for an unknown relation name | `services/catalog/src/catalog/api/v1/endpoints/access.py:147`, `services/catalog/src/catalog/api/v1/endpoints/access.py:205` *(+3 more)* |
| `catalog-api-11` | E8 | med | resilience | M | Cascade and enumeration paths await independent object-store I/O one item at a time | `services/catalog/src/catalog/api/v1/endpoints/tables.py:148`, `services/catalog/src/catalog/api/v1/endpoints/namespaces.py:227` *(+6 more)* |
| `catalog-api-12` | E1 | med | fga | S | The batch authorizer loops sequential fga.check calls for owner-tier operations instead of batch_check | `services/catalog/src/catalog/api/fga_deps.py:383`, `services/catalog/src/catalog/api/fga_deps.py:373` *(+1 more)* |
| `catalog-api-13` | E9 | med | testing | S | services/catalog/tests is missing from the explicit pytest testpaths, so five tests never run | `pyproject.toml:188`, `services/catalog/tests/test_commit_idempotency.py:1` |
| `catalog-api-06` | E1 | low | fga | M | Three tuple write/revoke call sites bypass the seed_ownership/revoke_ownership seam, each for a documented reason | `services/catalog/src/catalog/api/v1/endpoints/warehouses.py:473`, `services/catalog/src/catalog/api/v1/endpoints/projects.py:344` *(+3 more)* |
| `catalog-api-07` | E4 | low | resilience | S | _collect_descendants recurses with no depth cap and no cycle guard, while its sibling enumerator in the same service has both | `services/catalog/src/catalog/api/v1/endpoints/namespaces.py:54`, `services/catalog/src/catalog/api/v1/endpoints/namespaces.py:80` *(+2 more)* |
| `catalog-api-08` | E4 | low | error-handling | S | Bare `except Exception` around seed_ownership makes any programming error in the grant path look like an FGA outage | `services/catalog/src/catalog/api/v1/endpoints/data.py:258`, `services/catalog/src/catalog/api/v1/endpoints/data.py:262` *(+1 more)* |
| `catalog-api-14` | E6 | low | duplication | M | user_state.py and policies.py ship near-identical handler triples, four and three times over | `services/catalog/src/catalog/api/v1/endpoints/user_state.py:183`, `services/catalog/src/catalog/api/v1/endpoints/user_state.py:215` *(+8 more)* |
| `catalog-api-15` | E11 | low | typing | S | Annotated-Depends aliases are used as plain parameter annotations on functions that are not dependencies | `services/catalog/src/catalog/api/v1/endpoints/access.py:87`, `services/catalog/src/catalog/api/v1/endpoints/access.py:129` *(+7 more)* |
| `catalog-api-16` | E5 | low | fastapi | S | Constrained string values are passed as bare `str \| None` and re-parsed with ad-hoc .lower() comparisons instead of StrEnum | `services/catalog/src/catalog/api/v1/endpoints/data.py:122`, `services/catalog/src/catalog/api/v1/endpoints/data.py:210` *(+6 more)* |
| `catalog-api-17` | E5 | low | config | S | The lifespan mutates the @lru_cache'd Settings singleton in place to inject the S3 secret | `services/catalog/src/catalog/main.py:80`, `services/catalog/src/catalog/main.py:53` *(+1 more)* |
| `catalog-api-18` | E2 | low | fastapi | S | An async route performs uncached blocking file I/O to read the authorization model DSL | `services/catalog/src/catalog/api/v1/endpoints/access_admin.py:318`, `services/catalog/src/catalog/api/v1/endpoints/access_admin.py:67` *(+1 more)* |
| `catalog-api-19` | E7 | low | readability | S | create_table runs a parent-existence round trip before the cheap request-shape validations it should follow | `services/catalog/src/catalog/api/v1/endpoints/data.py:151`, `services/catalog/src/catalog/api/v1/endpoints/data.py:155` *(+3 more)* |
| `catalog-api-20` | E12 | low | dead-code | S | Two task-listing routes declare a CurrentToken dependency their bodies never use | `services/catalog/src/catalog/api/v1/endpoints/tables.py:479`, `services/catalog/src/catalog/api/v1/endpoints/namespaces.py:335` |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- pyproject.toml:188 — services/lineage/tests is ALSO absent from testpaths (same omission as services/catalog/tests in finding 13); both are the only services/*/tests dirs on disk missing from the explicit list.
- services/catalog/src/catalog/api/fga_deps.py:699 — revoke_ownership records the audit origin as origin="create" on a REVOKE (`fga.revoke_object_tuples(client, obj, actor=actor, origin="create")`), so every single-object drop/rename revoke is filed in the audit trail under the create vocabulary; the same file's seed_ownership uses origin="create" legitimately at :665.

### `catalog-core`

*Layout as it actually is:* The catalog uses sanctioned layout (b): fastapi-template `api/v1/endpoints/` + `core/` + `services/`, its own `FastAPI(...)` in `main.py`, and `core/config.py`. My scope is `core/` (config, namespace, identifiers, serialization, lineage_emit, lineage_metadata, control_emit, control_buffer, vending) and `services/` (dataplane, warehouses, projects, models, publication, maintenance, native) plus the one in-service test. Every service function is a plain sync `def` over `(ns, storage_options, request)` returning a `lance_namespace` response model, and I verified at every call site (columns/tables/data/maintenance/models/publication/credentials endpoints) that they are invoked through `run_in_threadpool` — so there is NO blocking-IO-in-async defect anywhere in this scope, and `vendor.vend`'s boto3 STS call is threadpooled too. Config is one `pydantic-settings` `Settings` (~60 fields, `LANCE_*` aliases, `SecretStr` for the S3 secret, two `@model_validator(mode="after")` fail-fast gates) behind `@lru_cache get_settings`. Error wiring is correct for the Lance-plane contract: the service layer raises `lance_namespace` typed errors (`InvalidInputError`, `TableNotFoundError`, `ConcurrentModificationError`, `ServiceUnavailableError`, `UnsupportedOperationError`, `TableColumnNotFoundError`, …) translated by `service_kit.lakehouse.ns_errors.install_problem_handlers` into RFC 9457…

*Tests:* `tests/unit/` covers most of this scope genuinely well — `test_vending.py` (incl. cross-tenant and sibling-isolation policy assertions), `test_warehouses.py`, `test_warehouse_registry.py`, `test_warehouse_delete.py`, `test_publication.py`, `test_model_promotion.py`, `test_model_artifacts.py`, `test_lineage_emit.py`, `test_column_lineage_emit.py`, `test_catalog_hierarchy_guard.py`. Four real gaps. (1) `services/catalog/tests/` is absent from the root `pyproject.toml` `testpaths`, so `test_commit_idempotency.py` — the only guard on the duplicate-append defect — is never collected by `make test`; I confirmed it collects 0 tests in the default run and 5 passing tests when invoked explicitly.…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `CAT-CORE-01` | E3 | **HIGH** | error-handling | S | Commit idempotency guard fails OPEN on any storage error, re-enabling the duplicate-append it exists to prevent | `services/catalog/src/catalog/services/dataplane.py:621`, `services/catalog/src/catalog/services/dataplane.py:623` *(+3 more)* |
| `CAT-CORE-03` | E9 | **HIGH** | testing | S | The catalog's in-service test directory is not in `testpaths`, so the commit-idempotency tests never run in CI | `pyproject.toml:188`, `services/catalog/tests/test_commit_idempotency.py:1` |
| `CAT-CORE-02` | E1 | med | security | S | STS session policy is built by unescaped interpolation — a wildcard in a table name widens vended credentials to sibling tables | `services/catalog/src/catalog/core/vending.py:103`, `services/catalog/src/catalog/core/vending.py:105` *(+4 more)* |
| `CAT-CORE-04` | E7 | med | structure | L | `dataplane.py` is a 1438-line God module carrying twelve unrelated concerns, including HTTP protocol semantics | `services/catalog/src/catalog/services/dataplane.py:155`, `services/catalog/src/catalog/services/dataplane.py:432` *(+8 more)* |
| `CAT-CORE-05` | E3 | med | resilience | M | Control-plane registry writes are plain overwrites with no compare-and-swap — concurrent updates are silently lost | `services/catalog/src/catalog/services/warehouses.py:78`, `services/catalog/src/catalog/services/warehouses.py:177` *(+3 more)* |
| `CAT-CORE-06` | E4 | med | error-handling | S | The model registry collapses every `OSError` to 404, reporting a store outage as "model not found" | `services/catalog/src/catalog/services/models.py:47`, `services/catalog/src/catalog/services/models.py:113` *(+3 more)* |
| `CAT-CORE-07` | E6 | med | duplication | M | An 11-keyword emitter signature is hand-copied seven times across the lineage emission layer | `services/catalog/src/catalog/core/lineage_emit.py:278`, `services/catalog/src/catalog/core/lineage_emit.py:293` *(+5 more)* |
| `CAT-CORE-08` | E11 | med | typing | M | Service functions return `dict[str, Any]` that endpoints splat into Pydantic models, so the boundary is unchecked | `services/catalog/src/catalog/services/maintenance.py:64`, `services/catalog/src/catalog/services/maintenance.py:91` *(+8 more)* |
| `CAT-CORE-09` | E8 | med | resources | M | Each mutating table op performs three full namespace describes plus three dataset opens | `services/catalog/src/catalog/services/dataplane.py:94`, `services/catalog/src/catalog/services/dataplane.py:1061` *(+7 more)* |
| `CAT-CORE-10` | E8 | med | resources | S | The commit path issues unbounded serial object-store round trips — one HEAD per data file, one transaction read per version | `services/catalog/src/catalog/services/dataplane.py:702`, `services/catalog/src/catalog/services/dataplane.py:710` *(+2 more)* |
| `CAT-CORE-11` | E6 | med | coupling | S | The warehouse registry hand-rolls a raw boto3 S3 client instead of the canonical `packages/storage` wrapper | `services/catalog/src/catalog/services/warehouses.py:35`, `services/catalog/src/catalog/services/warehouses.py:41` *(+3 more)* |
| `CAT-CORE-12` | E11 | med | typing | M | Three object-store registries parse JSON into raw dicts and hand-validate with `.get()` truthiness checks | `services/catalog/src/catalog/services/warehouses.py:126`, `services/catalog/src/catalog/services/warehouses.py:221` *(+4 more)* |
| `CAT-CORE-13` | E5 | med | config | M | A single 340-line `Settings` class carries every domain's configuration | `services/catalog/src/catalog/core/config.py:21`, `services/catalog/src/catalog/core/config.py:155` *(+6 more)* |
| `CAT-CORE-14` | E6 | low | coupling | S | The project registry imports two private helpers across a module boundary | `services/catalog/src/catalog/services/projects.py:24`, `services/catalog/src/catalog/services/warehouses.py:78` *(+1 more)* |
| `CAT-CORE-15` | E11 | low | typing | S | Maintenance ops type their dataset parameter as `Any` where a small Protocol would state the contract (style, not a gate violation) | `services/catalog/src/catalog/services/maintenance.py:21`, `services/catalog/src/catalog/services/maintenance.py:53` *(+4 more)* |
| `CAT-CORE-16` | E11 | low | typing | S | `@dataclass` value object and a duplicated legacy type alias in the data plane | `services/catalog/src/catalog/services/dataplane.py:724`, `services/catalog/src/catalog/services/dataplane.py:20` *(+2 more)* |
| `CAT-CORE-17` | E7 | low | readability | S | `create_table` is a 36-line pass-through that forwards every argument to `_create_table_direct` | `services/catalog/src/catalog/services/dataplane.py:155`, `services/catalog/src/catalog/services/dataplane.py:180` *(+1 more)* |
| `CAT-CORE-18` | E10 | low | observability | S | The OpenLineage `producer` URI stamped on every emitted event points at the wrong repository and a non-existent path | `services/catalog/src/catalog/core/lineage_emit.py:107`, `services/catalog/src/catalog/core/lineage_emit.py:198` *(+1 more)* |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- pyproject.toml:188 — services/lineage/tests is absent from testpaths for the same reason as services/catalog/tests: its two files (test_external_source_authz.py, test_privileged_identity.py) are never collected by `uv run pytest`, so the omission is a 2-directory pattern, not a 1-off.
- services/catalog/src/catalog/services/warehouses.py:86 — `_read_json` returns `json.loads(...)` unchecked while annotated `dict[str, str] | None`: a corrupt object decoding to a list/scalar flows into `warehouse_status`/`get_project` and 500s on `record.get(...)`, and a malformed body raises JSONDecodeError uncaught — the single-record read path has none of the isinstance/try tolerance `list_warehouses` (120-129) and `list_projects` (71-79) apply.

### `ingest-flow`

*Layout as it actually is:* `services/ingest` is a flat-module fleet service (sanctioned layout (a)): `ingest/__init__.py::create_app()` composes `service_kit.make_service_app` with the health/queue-health/api routers, an injected `_lifespan` that constructs a `dapr.ext.workflow.WorkflowRuntime` and calls `ingest.workflow.register()`, wires FGA/OIDC onto `app.state`, and installs `service_kit.lakehouse.ns_errors.install_problem_handlers`. My slice is the orchestration core, split by intent rather than by layer: `workflow.py` holds the two generator workflows (`ingest_run` parent → `emit_start`/`ensure_dataset`/`enumerate_chunks`/fan-out/`finalize`/`emit_terminal`; `chunk_run` child → `publish_units`/`drain_chunk`/`reconcile_chunk`) plus eight thin activity stubs that lazily import their side effects; `runtime.py` is the I/O layer (the bronze `pa.schema`, NATS connect/publish/drain/reconcile/release, `finalize_run`'s commit + publication); `worker.py` is the JetStream pull consumer that runs *inside* the `drain_chunk` activity (batch fetch → concurrent fetch/validate → one Lance fragment → stage → batch ack, with an `in_progress` heartbeat task); `queue.py` is the single sanctioned `nats` importer (stream/consumer config, `UnitTask`, dedupe id, DLQ park, `release_run`, `inspect_queue`, and a `QueueMessage` structural Protocol); `staging.py` is the pre-commit fragment ledger on S3/FS with exact-cover…

*Tests:* 35 files under `services/ingest/tests`, densely aimed at previously-shipped defects: `test_staging.py` + `test_partial_ack_duplication.py` (manifest keying, exact cover, the four-in/six-out duplication), `test_fragment_batching.py`, `test_heartbeat.py` (`in_progress`), `test_run_deadline.py` / `test_run_error_boundary.py` / `test_empty_source.py` / `test_empty_commit.py` (workflow terminal paths, driven by hand-stepping the generator), `test_run_status.py` (`merge_workflow_state`), `test_lander.py` / `test_partition_index.py` / `test_blob_read_apis.py`, `test_worker_queue.py`, `test_queue_health.py`, and an AST gate (`test_no_unread_publishes.py`) proving `signal_drained` stays deleted.…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `ingest-flow-01` | E3 | **HIGH** | dapr-events | M | Workflow control flow branches on import-time os.getenv values — a replay on a pod with different env changes the recorded task sequence | `services/ingest/src/ingest/workflow.py:67`, `services/ingest/src/ingest/workflow.py:82` *(+6 more)* |
| `ingest-flow-02` | E3 | **HIGH** | resilience | S | Three of four NATS connect sites have no timeout, against the file's own measured evidence that a connect to a dead broker never returns | `services/ingest/src/ingest/runtime.py:192`, `services/ingest/src/ingest/runtime.py:232` *(+2 more)* |
| `ingest-flow-03` | E8 | **HIGH** | resources | L | discover_staged's exact-cover search is super-linear in the run's whole unit universe and recurses once per fragment — it cannot finish the million-unit run… | `services/ingest/src/ingest/staging.py:191`, `services/ingest/src/ingest/staging.py:192` *(+3 more)* |
| `ingest-flow-04` | E3 | **HIGH** | dapr-events | M | The run-deadline path abandons its child workflows and then purges the queue underneath them | `services/ingest/src/ingest/workflow.py:295`, `services/ingest/src/ingest/workflow.py:296` *(+3 more)* |
| `ingest-flow-05` | E3 | med | resilience | S | Transient fetch failures nak with no delay and max_deliver exhaustion is never detected — the DLQ the module docstring promises is unreachable for the common… | `services/ingest/src/ingest/worker.py:279`, `services/ingest/src/ingest/worker.py:272` *(+2 more)* |
| `ingest-flow-06` | E3 | med | error-handling | S | park_poison publishes unguarded — one bad unit fails the whole run when the DLQ stream is absent | `services/ingest/src/ingest/worker.py:275`, `services/ingest/src/ingest/worker.py:376` *(+2 more)* |
| `ingest-flow-07` | E3 | med | observability | S | No trace context crosses the queue boundary; UnitTask.traceparent is a declared-but-never-written field | `services/ingest/src/ingest/queue.py:84`, `services/ingest/src/ingest/queue.py:216` *(+2 more)* |
| `ingest-flow-08` | E3 | med | error-handling | S | Every chunk's reconcile error uses the literal key "__chunk__", and the parent flattens all chunks into one dict — N failures collapse to one | `services/ingest/src/ingest/runtime.py:299`, `services/ingest/src/ingest/workflow.py:314` *(+2 more)* |
| `ingest-flow-09` | E5 | med | config | M | Operational tunables read via scattered os.getenv with no settings singleton — the plane already uses pydantic-settings for auth but not for… | `services/ingest/src/ingest/runtime.py:145`, `services/ingest/src/ingest/runtime.py:152` *(+8 more)* |
| `ingest-flow-10` | E7 | med | readability | M | drain_chunk and finalize_run are oversized multi-purpose functions with nested closures and a coroutine redefined inside a loop | `services/ingest/src/ingest/worker.py:289`, `services/ingest/src/ingest/worker.py:333` *(+3 more)* |
| `ingest-flow-11` | E6 | med | coupling | M | The catalog seam is duck-typed by hasattr/getattr while a Protocol exists that describes a different, smaller contract | `services/ingest/src/ingest/runtime.py:393`, `services/ingest/src/ingest/runtime.py:450` *(+3 more)* |
| `ingest-flow-12` | E7 | med | readability | M | The scope is ~45-58% prose (52% overall, measured), and the prose is PR narrative — dated measurements, named test files, and a changelog of past defects | `services/ingest/src/ingest/runtime.py:29`, `services/ingest/src/ingest/lander.py:147` *(+5 more)* |
| `ingest-flow-13` | E8 | med | resources | S | A boto3 client is constructed per staging call (twice per read/purge), and the S3 response bodies are never closed | `services/ingest/src/ingest/staging.py:310`, `services/ingest/src/ingest/staging.py:104` *(+3 more)* |
| `ingest-flow-14` | E3 | med | resilience | S | publish_units awaits one JetStream publish per unit and discards the PubAck, so a chunk is 1000 sequential round-trips and dedupes are reported as accepted | `services/ingest/src/ingest/queue.py:214`, `services/ingest/src/ingest/queue.py:216` *(+1 more)* |
| `ingest-flow-15` | E8 | med | resources | S | InMemoryRunStore grows without bound and re-sorts the whole map on every recent() call | `services/ingest/src/ingest/runs.py:100`, `services/ingest/src/ingest/runs.py:105` *(+1 more)* |
| `ingest-flow-16` | E4 | low | typing | S | Generator workflows annotated as returning their final value, plus Any seams and a stale type-ignore that the QueueMessage Protocol already covers | `services/ingest/src/ingest/workflow.py:170`, `services/ingest/src/ingest/workflow.py:341` *(+7 more)* |
| `ingest-flow-17` | E3 | low | error-handling | S | ensure_stream treats every add_stream exception as "already exists" and logs it at DEBUG | `services/ingest/src/ingest/queue.py:148`, `services/ingest/src/ingest/queue.py:151` *(+2 more)* |
| `ingest-flow-18` | E12 | low | dead-code | S | Unreferenced helper and a legacy field still written on every manifest | `services/ingest/src/ingest/lander.py:235`, `services/ingest/src/ingest/staging.py:98` |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- services/ingest/src/ingest/workflow.py:325 — the `except Exception` branch of `ingest_run` has the identical child-abandonment defect as finding 04's deadline path, and it is reachable on the DEFAULT config: `when_all` raises as soon as one `chunk_run` child fails, while every other child is still durably executing inside `drain_chunk`, and the handler goes straight to `emit_terminal` (:337) → `release_run_units` → `purge_stream` + `delete_consumer` on the shared durable those live children are pulling from. Finding 04 scopes the problem to `MAX_RUN_HOURS > 0`; it is not conditional on the ceiling at all.
- services/ingest/src/ingest/worker.py:365 — `UnitTask.model_validate_json(msg.data)` is the first statement of `handle` and is outside every try/except (the `try` at :367 wraps only `self._one`). A malformed or truncated message body raises `ValidationError` out of `asyncio.gather(*(handle(m) for m in fresh))` (:390), out of `drain_chunk`, burns all four `ACTIVITY_RETRY` attempts against a message that will be redelivered identically, and fails the whole run — and the offending message is never acked, never naked, and never parked, so it is not in `outcome.errors` either. Same 'one bad unit fails the run' class as finding 06, different trigger.

### `ingest-domain`

*Layout as it actually is:* `services/ingest` is a **fleet flat-module** service (sanctioned layout (a)): `ingest/__init__.py::create_app()` composes `service_kit.make_service_app(title="ingest", routers=[health, queue_health, ingest], lifespan=_lifespan)`, then bolts on `install_problem_handlers(app, logger)` from `service_kit.lakehouse.ns_errors` so the auth door's `lance_namespace` typed errors become RFC 9457 problem+json. Routers carry `tags=` but no `prefix=`; `make_service_app` mounts every router under `Settings.api_prefix` (chart + `dev-micro.sh` set `RASK_API_PREFIX=/api`, so the real paths are `/api/ingests`, `/api/ingests/{run_id}`, `/api/sources`, `/api/health`, `/api/queue`, verified against `app.openapi()`), and the gateway row `("/api/ingest", "/api", "ingest", …)` fronts them. There is **no `core/config.py` and no service `BaseSettings`** — configuration is 22 scattered `os.getenv`/`os.environ` reads across 9 of the 15 files in scope, several evaluated at import time; the one Pydantic settings model is `IngestAuthSettings(GovernedAuthSettings, BaseSettings)` in `auth.py`, uncached. DI is `app.state` + tiny `Request`-reading dep functions (`get_store`/`get_starter`/`get_reader`) consumed via `Annotated[..., Depends(...)]`; the run store, workflow starter/reader, provenance reader, FGA client and OIDC verifier are attached in `create_app`/`_wire_auth`, with an async `_resolve_fga_client`…

*Tests:* Coverage is unusually good for this estate: 34 test files / ~300 tests, each named after the invariant it pins. Strong spots — path confinement (`test_local_dir_confinement.py`, 9 tests, asserts refusal at BOTH the adapter and the `UriFetcher` seam, parameterised over `/proc/self`, the service-account token, `/etc`, `/`, plus `..` traversal and percent-encoded traversal); auth (`test_ingest_auth.py`, 14); run-status merge (`test_run_status.py`, 27); the catalog HTTP client (`test_catalog_service.py`, 19 + `test_catalog_token.py`, 5); provenance refusal (8); the source registry and `describe_sources` (`test_adapters.py` 9, `test_sources_endpoint.py` 4, `test_source_agnostic.py` 7).…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `ING-01` | E1 | **HIGH** | security | S | The ingest write door opens completely when APP_API_TOKEN is unset — and ingest is the only governed service with no startup guard closing that path | `services/ingest/src/ingest/auth.py:116`, `services/ingest/src/ingest/auth.py:117` *(+2 more)* |
| `ING-02` | E2 | **HIGH** | fastapi | S | Blocking OIDC discovery + JWKS network IO runs inside async def — every bearer-authenticated ingest request can stall the event loop for up to 15 s | `services/ingest/src/ingest/auth.py:151`, `services/ingest/src/ingest/api.py:127` *(+2 more)* |
| `ING-04` | E5 | **HIGH** | config | M | s3-prefix builds a bare pyarrow S3FileSystem that ignores every RASK_S3_* knob — while its docstring claims it goes through the estate's provider-agnostic… | `services/ingest/src/ingest/adapters.py:101`, `services/ingest/src/ingest/adapters.py:115` |
| `ING-03` | E4 | med | error-handling | M | One router, two incompatible error contracts: four domain HTTPExceptions bypass the problem+json translator this app installs | `services/ingest/src/ingest/api.py:130`, `services/ingest/src/ingest/api.py:144` *(+2 more)* |
| `ING-05` | E1 | med | fga | M | GET /ingests authorizes with a loop of up to 200 sequential FGA checks instead of batch_check/list_objects | `services/ingest/src/ingest/api.py:243`, `services/ingest/src/ingest/api.py:246` *(+1 more)* |
| `ING-06` | E4 | med | error-handling | S | The same list loop swallows an FGA/authn outage as if it were a denial — an incident renders as an empty list at DEBUG | `services/ingest/src/ingest/api.py:249`, `services/ingest/src/ingest/api.py:253` |
| `ING-07` | E5 | med | config | M | No service settings model: 25 scattered os.getenv reads across 8 files, several frozen at import time, with one convention read three different ways | `services/ingest/src/ingest/fetch.py:43`, `services/ingest/src/ingest/fetch.py:112` *(+8 more)* |
| `ING-08` | E8 | med | resilience | M | A fresh HTTP client is built for every outbound call — including one per fetched unit, the exact defect this service's own docstring warns against | `services/ingest/src/ingest/fetch.py:83`, `services/ingest/src/ingest/catalog_service.py:233` *(+8 more)* |
| `ING-09` | E3 | med | duplication | S | _fetch_http is a near-verbatim copy of storage.iiif.fetch_image's retry loop, assert-for-control-flow included | `services/ingest/src/ingest/fetch.py:82`, `services/ingest/src/ingest/fetch.py:100` *(+1 more)* |
| `ING-10` | E4 | med | fastapi | S | The 202's Location header points at /v1/ingests/{run_id} — a path this service never serves under any prefix | `services/ingest/src/ingest/api.py:155`, `services/ingest/src/ingest/api.py:177` *(+2 more)* |
| `ING-12` | E4 | med | fastapi | M | authorize_ingest is a pseudo-dependency: its Header/Depends annotations are inert because all three call sites invoke it positionally inside route bodies | `services/ingest/src/ingest/auth.py:98`, `services/ingest/src/ingest/api.py:127` *(+2 more)* |
| `ING-13` | E5 | med | config | S | get_auth_settings is uncached, so every request re-instantiates a BaseSettings that reads .env from disk — and then hand-patches a field from os.environ | `services/ingest/src/ingest/auth.py:72`, `services/ingest/src/ingest/auth.py:74` *(+2 more)* |
| `ING-14` | E3 | med | resilience | M | The A8 provenance check fetches the estate's entire unbounded /runs board and linear-scans it on every status read of a completed run | `services/ingest/src/ingest/provenance.py:97`, `services/ingest/src/ingest/provenance.py:115` *(+1 more)* |
| `ING-11` | E8 | low | resources | S | The OpenFGA client is built on app.state and never closed — the lifespan's cleanup block disposes only the workflow runtime | `services/ingest/src/ingest/__init__.py:85`, `services/ingest/src/ingest/__init__.py:163` *(+1 more)* |
| `ING-15` | E11 | low | typing | M | The source registry stores its callables as `object` and casts them back with three `# type: ignore[assignment]`; four public functions return bare `Any` | `services/ingest/src/ingest/sources.py:100`, `services/ingest/src/ingest/sources.py:151` *(+6 more)* |
| `ING-16` | E12 | low | dead-code | S | Three dead entry points, two of them carrying docstrings that assert they are load-bearing | `services/ingest/src/ingest/lineage.py:69`, `services/ingest/src/ingest/sources.py:193` *(+1 more)* |
| `ING-17` | E1 | low | error-handling | S | The queue diagnostic returns raw exception text in its response body, and the catalog's existence probe treats any exception as 'absent' | `services/ingest/src/ingest/queue_health.py:125`, `services/ingest/src/ingest/catalog.py:69` *(+1 more)* |
| `ING-18` | E7 | low | readability | S | Query-parameter clamping done by hand instead of declared, and a frozen-model idiom used on a mutable model | `services/ingest/src/ingest/api.py:232`, `services/ingest/src/ingest/api.py:243` *(+1 more)* |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- services/ingest/src/ingest/api.py:255 — list_ingests builds RunStatusResponse straight from the stored record with no merge_workflow_state()/engine read, so every listed run reports ACCEPTED forever (the store is never written back after get_ingest merges at :296). This is precisely the defect api.py:291-293 says was fixed for the single-run path, re-shipped one route up.
- services/ingest/src/ingest/api.py:283 — get_ingest raises 404 for an unknown run BEFORE authorize_ingest runs (:289), so an unauthenticated/unauthorized caller can distinguish an existing run id from a non-existent one (404 vs 403). run_id_for is a deterministic hash of (project, idempotency-key), so ids are guessable, and the same file argues at :239-241 that leaking run existence is what the per-record filter exists to prevent.
- services/ingest/src/ingest/api.py:274 — the workflow-engine read (asyncio.to_thread(reader.state, run_id)) is performed before any authorization check, letting an unauthenticated caller drive a Dapr gRPC lookup per request.

### `annotator`

*Layout as it actually is:* `services/annotator` (37 .py, ~6.6k LOC, no local `tests/`) is the fastapi-template layout (sanctioned layout (b)): a thin module-level `main.py` builds `app = FastAPI(title=..., lifespan=lifespan)`, constructs everything onto `app.state.resources` (a `service_kit.media.state.AppState` carrying a **sync** `httpx.Client`, the Lance `DatasetRegistry` and caches) inside an `@asynccontextmanager` lifespan, registers `service_kit.exceptions.register_handlers` + `service_kit.lakehouse.ns_errors.install_problem_handlers` (RFC 9457 problem+json) and CORS via `service_kit.media.middleware.register_middleware`, mounts `service_kit.probes` for `/livez`/`/readyz` gated on `startup_complete`/`shutting_down`, and mounts `dapr.ext.fastapi.DaprActor` at import (routes only) with actor **registration** deferred into the lifespan. Config is `AnnotatorSettings(GovernedAuthSettings, Settings)` + `@lru_cache get_annotator_settings()` — zero `os.getenv` anywhere in the service. DI is `Annotated[...]` aliases throughout (`StateDep`, `AuthorDep`, `DatasetParam` from service-kit; `CheckerDep`, `CurrentSubject`, `RawBearerToken`, `FgaClientDep` from `api/security.py`), with OIDC + OpenFGA both fail-closed to 503 when enabled-but-unbuilt. Two planes coexist: (1) the **Lance data plane** — `annotations/{wire,save,tags,versions,commit,schema}.py`, all correctly plain `def` routes serving Arrow IPC and…

*Tests:* There is no `services/annotator/tests`; the suite lives in `tests/unit` (29 files import `annotator.`) and is genuinely strong for the pure domain: `test_annotation_projects_machine.py`, `test_label_ontology.py`, `test_annotation_publish.py`, `test_publish_saga.py`, `test_agreement.py`, `test_annotate.py` (21 tests over wire/save/tags/versions incl. real tmp_path Lance round-trips), the two actor tests, `test_catalog_publisher.py` (incl. `publish_token` minting + IdP refusal), and endpoint tests for projects/members/tasks/events/ontology-patch. Thin or absent: (1) `api/v1/endpoints/jobs.py` has **no test at all** — the `_remote`/`_remote_status` 503 contract and `job_id_for` determinism…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `ANN-01` | E2 | **HIGH** | resources | S | Blocking Lance/S3 dataset resolution runs on the event loop inside async routes | `services/annotator/src/annotator/api/v1/endpoints/assist.py:217`, `services/annotator/src/annotator/api/v1/endpoints/project_events.py:332` *(+2 more)* |
| `ANN-02` | E3 | **HIGH** | resilience | M | Publish saga is fire-and-forget with a module-global in-flight set — a lost task strands the project in `publishing` forever | `services/annotator/src/annotator/projects/lakehouse.py:277`, `services/annotator/src/annotator/projects/lakehouse.py:280` *(+2 more)* |
| `ANN-03` | E8 | **HIGH** | resources | M | Per-item actor round-trips are awaited sequentially on send, publish and list — up to 2000 serialised sidecar calls in one request | `services/annotator/src/annotator/api/v1/endpoints/project_events.py:457`, `services/annotator/src/annotator/api/v1/endpoints/project_events.py:464` *(+6 more)* |
| `ANN-04` | E5 | med | config | S | CORS `allow_methods` omits PUT/PATCH/DELETE while seven routes serve exactly those methods | `services/annotator/src/annotator/main.py:125`, `services/annotator/src/annotator/api/v1/endpoints/tasks.py:247` *(+6 more)* |
| `ANN-05` | E4 | med | error-handling | M | Ontology enforcement fails open on the two BULK-WRITE paths (send + import) when a stored ontology will not parse | `services/annotator/src/annotator/api/v1/endpoints/project_events.py:368`, `services/annotator/src/annotator/api/v1/endpoints/tasks.py:329` *(+2 more)* |
| `ANN-06` | E3 | med | error-handling | M | Domain errors cross the actor boundary as formatted strings reconstructed by regex | `services/annotator/src/annotator/projects/proxies.py:60`, `services/annotator/src/annotator/projects/proxies.py:73` *(+2 more)* |
| `ANN-07` | E11 | med | fastapi | M | Half the routes return bare `dict[str, Any]` — raw actor documents reach clients with no schema or filtering | `services/annotator/src/annotator/api/v1/endpoints/tasks.py:165`, `services/annotator/src/annotator/api/v1/endpoints/tasks.py:237` *(+8 more)* |
| `ANN-08` | E8 | med | resources | M | Version history opens and scans one Lance snapshot per version, up to 200 per request | `services/annotator/src/annotator/annotations/versions.py:89`, `services/annotator/src/annotator/annotations/versions.py:91` |
| `ANN-09` | E8 | med | resources | S | `project_facet` rescans every published row once per distinct task — quadratic on the publish path | `services/annotator/src/annotator/projects/publish.py:352`, `services/annotator/src/annotator/projects/publish.py:353` |
| `ANN-10` | E3 | med | resilience | L | Multi-write create/send sequences have no compensation — a mid-sequence failure leaves an unusable project and relies on the caller retrying | `services/annotator/src/annotator/api/v1/endpoints/projects.py:105`, `services/annotator/src/annotator/api/v1/endpoints/projects.py:110` *(+3 more)* |
| `ANN-12` | E1 | med | security | S | Arrow-IPC import reads and materializes an unbounded request body | `services/annotator/src/annotator/api/v1/endpoints/tasks.py:335`, `services/annotator/src/annotator/projects/imports.py:144` *(+1 more)* |
| `ANN-13` | E6 | med | structure | M | Route prefixes are inconsistent inside one service — `/api`, `/api/jobs` and bare `/projects` / `/tasks` | `services/annotator/src/annotator/annotations/router.py:14`, `services/annotator/src/annotator/api/v1/endpoints/assist.py:31` *(+5 more)* |
| `ANN-14` | E3 | med | resilience | M | Publish transport builds a fresh httpx connection per call and retries on only one of its two paths | `services/annotator/src/annotator/projects/lakehouse.py:100`, `services/annotator/src/annotator/projects/lakehouse.py:142` *(+1 more)* |
| `ANN-11` | E8 | low | resources | M | Actor documents grow without bound and are fully re-serialized on every event | `services/annotator/src/annotator/projects/actor.py:171`, `services/annotator/src/annotator/projects/actor.py:212` *(+3 more)* |
| `ANN-15` | E11 | low | typing | S | `@dataclass` on a value object where the house rule is Pydantic | `services/annotator/src/annotator/projects/saga.py:38`, `services/annotator/src/annotator/projects/saga.py:93` |
| `ANN-16` | E11 | low | typing | S | `Any` used for collaborators that already have a declared Protocol | `services/annotator/src/annotator/api/v1/endpoints/project_events.py:158`, `services/annotator/src/annotator/api/v1/endpoints/project_events.py:165` *(+7 more)* |
| `ANN-17` | E7 | low | readability | S | Throwaway class as a response stand-in, and a doubled `.json()` parse, in the publish transport | `services/annotator/src/annotator/projects/lakehouse.py:155`, `services/annotator/src/annotator/projects/lakehouse.py:103` |
| `ANN-18` | E12 | low | dead-code | S | Shutdown closes resource slots the annotator never populates | `services/annotator/src/annotator/main.py:104` |
| `ANN-19` | E7 | low | structure | S | Function-local stdlib imports beyond the cases the convention justifies | `services/annotator/src/annotator/api/v1/endpoints/project_events.py:499`, `services/annotator/src/annotator/projects/project_actor.py:380` *(+2 more)* |
| `ANN-20` | E7 | low | fastapi | S | Member grant/revoke uses DELETE with a required request body | `services/annotator/src/annotator/api/v1/endpoints/members.py:136` |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- services/annotator/src/annotator/main.py:44 — `dataset_handle(state)` (sync store.exists + lancedb.connect, S3) is called directly inside the `async def lifespan`, the same HOUSE-RULE-4 blocking-IO-on-the-loop shape as ANN-01, in a file the auditor cited twice; it blocks startup and any concurrently-served probe for the duration of the S3 open.
- services/annotator/src/annotator/api/v1/endpoints/tasks.py:153-157 — `_refuse_second_replica` awaits `_proxy(sibling_id).get()` one sibling at a time inside a `for k in range(1, consensus_n + 1)` loop (plus the project read at :151); same sequential-independent-IO pattern as ANN-03, bounded at 4 extra round-trips but on the hot claim/assign path.

### `lineage`

*Layout as it actually is:* `services/lineage` follows sanctioned layout (b) — the fastapi-template shape — with `src/lineage/{main,models,schemas,seed}.py` plus `api/` (`dependencies.py`, `security.py`, `fga_deps.py`, `dapr.py`, `reconcile_cron.py`, `v1/router.py`, `v1/endpoints/{datasets,discovery,columns,governance,runs,reconcile,ingest,dlq,demo}.py`), `core/` (`config.py`, `age.py`, `reconcile.py`, `metrics.py`) and `services/` (`repository.py`, `consumer.py`). Entrypoint is a module-level `app = FastAPI(...)` in `main.py` with an `@asynccontextmanager` lifespan that fetches Dapr secrets via `run_in_threadpool`, builds the psycopg `AsyncConnectionPool` (`core/age.make_pool`), constructs `LineageRepository`, runs four idempotent bootstrap DDL awaits, and conditionally wires an `OIDCVerifier` + OpenFGA client onto `app.state`; `register_dapr(app)`, `install_problem_handlers(app, log)`, `make_probes_router(_graph_ready)`, an optional demo router, an optional cron router and a `/ui` StaticFiles mount follow at import time. Config is a single `LineageSettings(BaseSettings)` with `LINEAGE_*`/`LANCE_*` aliases, a fail-closed `@model_validator`, and `@lru_cache get_settings()`. Error wiring is correct per HOUSE-RULE-5: zero `HTTPException` in the service — every domain error is a `lance_namespace` typed error (`InvalidInputError`, `TableNotFoundError`, `TransactionNotFoundError`, `PermissionDeniedError`,…

*Tests:* Two suites live in-tree (`services/lineage/tests/test_external_source_authz.py`, `test_privileged_identity.py`, 18 tests) and are NOT collected by the root `testpaths` — verified with `uv run pytest --collect-only` (0 matches) versus a direct invocation (18 collected). The service is otherwise covered from the central `tests/unit/` tree, which is substantial: `test_lineage.py`, `test_lineage_auth.py`, `test_lineage_governance.py`, `test_lineage_discovery.py`, `test_lineage_demo.py`, `test_lineage_dapr_delivery.py`, `test_consumer.py`, `test_reconcile.py`, `test_dlq_ops.py`, `test_events_parity.py`, `test_openlineage_spec_conformance.py`, plus `tests/e2e-py/test_lineage_e2e.py` and…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `F-LIN-01` | E9 | **HIGH** | testing | S | services/lineage/tests is absent from root testpaths — 18 tests, including two security regressions, never run | `pyproject.toml:188`, `services/lineage/tests/test_privileged_identity.py:45` *(+1 more)* |
| `F-LIN-02` | E5 | **HIGH** | config | S | The Dapr secret store is addressed by two different env-var names — the settings one is ignored on the auth path | `services/lineage/src/lineage/api/security.py:85`, `services/lineage/src/lineage/api/security.py:86` *(+2 more)* |
| `F-LIN-03` | E7 | med | structure | L | repository.py is a 1382-line God module mixing a Cypher DSL, two storage surfaces, DDL bootstrap, cluster coordination and event synthesis | `services/lineage/src/lineage/services/repository.py:86`, `services/lineage/src/lineage/services/repository.py:449` *(+5 more)* |
| `F-LIN-04` | E8 | med | resilience | M | Every list/browse read is an unbounded fetch-all with no server-side LIMIT, on endpoints documented as polled every 2s | `services/lineage/src/lineage/services/repository.py:111`, `services/lineage/src/lineage/services/repository.py:118` *(+6 more)* |
| `F-LIN-05` | E2 | med | resilience | S | Uncached blocking secret-store fetch with a ~2-minute retry budget runs on every privileged-service request | `services/lineage/src/lineage/api/security.py:75`, `services/lineage/src/lineage/api/security.py:87` *(+2 more)* |
| `F-LIN-07` | E11 | med | typing | M | Domain values cross the models→repository boundary as untyped dicts and positional tuples while typed Pydantic twins exist on the read path | `services/lineage/src/lineage/models.py:68`, `services/lineage/src/lineage/models.py:89` *(+8 more)* |
| `F-LIN-08` | E7 | med | structure | M | Route topology is decided at import time by four settings-conditional module-level gates, none drivable from a test | `services/lineage/src/lineage/main.py:37`, `services/lineage/src/lineage/main.py:122` *(+3 more)* |
| `F-LIN-10` | E7 | med | readability | M | The cron sweep is a 114-line function, and reconcile_all serially awaits three independent round-trips per dataset | `services/lineage/src/lineage/api/reconcile_cron.py:41`, `services/lineage/src/lineage/core/reconcile.py:169` *(+4 more)* |
| `F-LIN-11` | E4 | med | error-handling | S | Three un-logged `except Exception` swallows in demo.py beside an unused logger, and a BaseException catch that misreports MemoryError as an unreadable dataset | `services/lineage/src/lineage/api/v1/endpoints/demo.py:31`, `services/lineage/src/lineage/api/v1/endpoints/demo.py:51` *(+4 more)* |
| `F-LIN-06` | E6 | low | duplication | S | prune_runs' batch size is duplicated as a Python constant and a literal baked into the Cypher — a change to one silently under-prunes | `services/lineage/src/lineage/services/repository.py:291`, `services/lineage/src/lineage/services/repository.py:292` *(+1 more)* |
| `F-LIN-09` | E8 | low | resources | S | The AGE pool leaks if any of the seven lifespan bootstrap steps fails after pool.open() | `services/lineage/src/lineage/main.py:62`, `services/lineage/src/lineage/main.py:81` *(+2 more)* |
| `F-LIN-12` | E12 | low | dead-code | S | An unused module constant, and a 45-line 'consumer side' of the run hierarchy that no production code reads | `services/lineage/src/lineage/main.py:38`, `services/lineage/src/lineage/models.py:290` *(+2 more)* |
| `F-LIN-13` | E1 | low | fga | S | A loop of sequential single check() calls where the module's own batch_check filter is available, and a duplicate-laden batch payload | `services/lineage/src/lineage/api/v1/endpoints/demo.py:148`, `services/lineage/src/lineage/api/v1/endpoints/columns.py:61` *(+1 more)* |
| `F-LIN-14` | E7 | low | structure | S | Only one of eight routers under api/v1/endpoints/ carries a version prefix, producing a double-/api path for ingest | `services/lineage/src/lineage/api/v1/endpoints/ingest.py:22`, `services/lineage/src/lineage/api/v1/endpoints/datasets.py:21` *(+4 more)* |
| `F-LIN-15` | E8 | low | resources | S | Module-level mutable caches in the demo endpoint, with a test-only reset seam instead of app.state | `services/lineage/src/lineage/api/v1/endpoints/demo.py:75`, `services/lineage/src/lineage/api/v1/endpoints/demo.py:76` *(+1 more)* |
| `F-LIN-16` | E11 | low | typing | S | Generic return widened to Any internally, bare list defaults on two Pydantic fields, and unannotated conn params | `services/lineage/src/lineage/api/fga_deps.py:246`, `services/lineage/src/lineage/schemas.py:36` *(+4 more)* |

### `medallion`

*Layout as it actually is:* `services/medallion` is the fastapi-template layout (house rule 6b) hosting TWO FastAPI apps out of one package: `medallion/producer.py` (the bronze ingest head + `/produce`, `/ingest-media`, `/train`, and the `/bronze-arrival` + `/publication-arrival` + `/train-trigger` + `/dlq-event` Dapr subscriptions) and `medallion/mover.py` (one DAG edge, `/medallion-event`), the latter deployed N times differing only by `MEDALLION_*` env. Both build their own `FastAPI(...)` with an `@asynccontextmanager` lifespan that sets `startup_complete`/`shutting_down`, fetches the S3 secret from the Dapr store via `run_in_threadpool(apply_dapr_secrets, ...)`, builds one `dapr.aio.DaprClient` + an optional `OpenFgaClient` (+ `OIDCVerifier` on the producer) onto `app.state`, and closes both under `suppress(Exception)` after `yield`; probes come from the shared `service_kit.probes` router and errors from `install_problem_handlers(app, log)` installed before any router (RFC 9457 problem+json, `lance_namespace` typed errors). Config is a single `MedallionSettings(BaseSettings)` (`core/config.py`, 343 lines, `MEDALLION_*` aliases, `@lru_cache get_settings`, a `model_validator(mode="after")` fail-fast pair, `SecretStr` for the S3 key). DI is correct `Annotated[T, Depends(...)]` aliases in `api/dependencies.py` (`SettingsDep`, `DaprClientDep`, `FgaClientDep`); every route carries a return type and…

*Tests:* `services/medallion/` ships NO tests directory and is absent from root `testpaths`; coverage lives at repo root — ~25 relevant files in `tests/unit/` (test_medallion_cascade, _compute, _derivers, _secrets, test_htr_parse/_stage/_register/_transcribe, test_publication_trigger, test_produce_auth, test_media_ingest, test_train, test_events_parity, test_column_lineage_emit, test_outbox_complete_survives_fail) plus `tests/e2e-py/test_medallion_e2e.py` (marker `medallion`, needs a live stack). Pure functions are well covered (ALTO parsing, derivers, column-map reconstruction, event wire parity). The thin spots are exactly where the findings land: (1) `tests/unit/test_publication_trigger.py:44`…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `MED-001` | E3 | **HIGH** | dapr-events | M | The publication cascade head puts the project-QUALIFIED NAMESPACE on the trigger's `project` field, so every real publication is DROPped by the mover | `services/medallion/src/medallion/services/publication_trigger.py:99`, `services/medallion/src/medallion/services/publication_trigger.py:122` *(+5 more)* |
| `MED-002` | E3 | **HIGH** | resilience | L | The in-process HTR lane holds the Dapr ack — and a process-wide asyncio.Lock — across the entire multi-page GPU transcription | `services/medallion/src/medallion/services/transform.py:225`, `services/medallion/src/medallion/services/transform.py:256` *(+3 more)* |
| `MED-003` | E1 | **HIGH** | security | M | The S3 secret key and the estate's APP_API_TOKEN are shipped into the Ray Jobs `runtime_env`, which the Jobs API echoes back to any reader | `services/medallion/src/medallion/services/ray_submit.py:68`, `services/medallion/src/medallion/services/ray_submit.py:159` *(+1 more)* |
| `MED-004` | E7 | med | structure | L | `handle_stage` is a 473-line God function that is the entire module — lane routing, tenant resolution, authz, compute dispatch, lineage, quality gate and four… | `services/medallion/src/medallion/services/transform.py:74` |
| `MED-005` | E6 | med | duplication | S | The "build a FAIL RunEvent and publish it through the outbox" block is copy-pasted four times inside `handle_stage` | `services/medallion/src/medallion/services/transform.py:401`, `services/medallion/src/medallion/services/transform.py:435` *(+2 more)* |
| `MED-006` | E3 | med | error-handling | S | Deterministic HTR page failures route to RETRY, re-running every GPU transcription in the batch — the sibling media lane already establishes the DROP contract… | `services/medallion/src/medallion/services/htr_stage.py:84`, `services/medallion/src/medallion/services/htr_transcribe.py:44` *(+3 more)* |
| `MED-007` | E3 | med | dead-code | M | The publication trigger's `from_version`/`to_version` range is published but never read — the mover still full-overwrites the tier, so the delta contract the… | `services/medallion/src/medallion/services/publication_trigger.py:112`, `services/medallion/src/medallion/services/publication_trigger.py:14` *(+2 more)* |
| `MED-008` | E8 | med | resources | M | Every outbound HTTP call builds its own httpx client — one connection pool opened and torn down per Ray submit, per catalog register, and per transcribed page | `services/medallion/src/medallion/services/ray_submit.py:107`, `services/medallion/src/medallion/services/ray_submit.py:186` *(+2 more)* |
| `MED-011` | E1 | med | fga | M | The medallion writes OpenFGA tuples directly from a message handler — a second tuple-write path outside the catalog's `seed_ownership` | `services/medallion/src/medallion/services/train.py:239`, `services/medallion/src/medallion/services/train.py:242` *(+1 more)* |
| `MED-012` | E6 | med | coupling | S | `htr_stage` imports four private names out of `compute`, coupling the HTR lane to the generic compute's internals | `services/medallion/src/medallion/services/htr_stage.py:27`, `services/medallion/src/medallion/services/compute.py:43` *(+1 more)* |
| `MED-009` | E5 | low | config | S | `APP_API_TOKEN` is read raw from `os.environ` in two unrelated modules instead of the typed settings surface (the other 11 reads are OTel env passthrough into… | `services/medallion/src/medallion/services/ray_submit.py:74`, `services/medallion/src/medallion/services/ray_submit.py:80` *(+5 more)* |
| `MED-010` | E1 | low | security | S | The service-token comparison (env read + dev-open + compare_digest) has a second home in `authorize_produce`, though the dual-auth fall-through means the… | `services/medallion/src/medallion/api/produce_auth.py:76`, `services/medallion/src/medallion/api/produce_auth.py:92` *(+2 more)* |
| `MED-013` | E11 | low | typing | S | Two handler seams are typed `Any` with an ANN401 suppression while every sibling handler is concretely typed | `services/medallion/src/medallion/services/publication_trigger.py:66`, `services/medallion/src/medallion/services/train.py:176` |
| `MED-014` | E7 | low | structure | S | Both app entrypoints read settings and configure logging at import time | `services/medallion/src/medallion/mover.py:36`, `services/medallion/src/medallion/mover.py:39` *(+2 more)* |
| `MED-015` | E12 | low | dead-code | S | Unreachable return, a stale line reference, a truncated docstring — plus one inline comment (not a docstring) whose open-count claim is contradicted | `services/medallion/src/medallion/services/ray_submit.py:199`, `services/medallion/src/medallion/services/ray_submit.py:35` *(+2 more)* |
| `MED-016` | E10 | low | observability | S | `record_quality_blocked` is incremented for an undecodable-media failure that ran no quality assertion | `services/medallion/src/medallion/services/transform.py:430`, `services/medallion/src/medallion/core/metrics.py:29` |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- services/medallion/src/medallion/core/config.py:189 — `ray_poll_interval_seconds` (:189) and `ray_job_timeout_seconds` (:195) are dead settings: a repo-wide grep finds no reader in any .py or chart file after A13 deleted ray_submit's completion poll. Same dead-code rule as MED-015/MED-007.
- services/medallion/src/medallion/core/config.py:190 — the comment block above `ray_job_timeout_seconds` still asserts 'The mover BLOCKS its Dapr handler until the job finishes ... that only wastes a duplicate poll (re-attach)', which ray_submit.py:93-109's submit-and-ack (A13) directly contradicts; it is also the source of MED-002's 30 s ack-window figure, while the chart actually renders ackWait 720s (dapr-component.yaml:142).
- services/medallion/src/medallion/services/publication_trigger.py:45 — the DELIMITER comment claims it is 'Hardcoded here rather than read from medallion settings because medallion has none', but `MedallionSettings.delimiter` exists (core/config.py:187, alias MEDALLION_DELIMITER) and htr_register/htr_stage compose catalog table ids from it. Docstring-lies plus the same two-sources-of-truth problem MED-011 flags from the other side.

### `viewer-search`

*Layout as it actually is:* Both services follow sanctioned layout (b) (fastapi-template): `src/<name>/main.py` holds a module-level `app` with an `@asynccontextmanager` lifespan that builds one `service_kit.media.state.AppState` (settings + a pooled `httpx.Client`) onto `app.state.resources`, does a fail-fast default-dataset open, and closes `state.http`/`embedder`/`reranker` after `yield`; `core/config.py` subclasses the shared `service_kit.media.config.Settings` with an `@lru_cache get_<name>_settings()` and one `VIEWER_PORT`/`SEARCH_PORT` alias (shared data-plane vars stay `MEDIA_*`); `api/dependencies.py` is a pure re-export of `Annotated` aliases from `service_kit.media.deps` (`StateDep`, `DatasetParam`, `AuthorDep`); `api/v1/router.py` aggregates `api/v1/endpoints/*.py` (11 modules / 32 routes for viewer, 1 module / 3 routes for search); framework-free logic lives under `services/`. Error wiring is shared: `service_kit.exceptions.register_handlers` for `DomainError` plus `service_kit.lakehouse.ns_errors.install_problem_handlers` for `LanceNamespaceError` → RFC 9457, with `register_middleware` adding only CORS (deliberately no `BaseHTTPMiddleware`, to keep 206 range streaming intact). DI is consistently `Annotated[T, Depends(...)]` aliases — no default-arg `Depends`. Viewer additionally carries `api/security.py`, which calls `service_kit.governed.deps.make_auth_deps(SettingsDep)` once at module…

*Tests:* Neither service ships a `tests/` directory, and neither `services/viewer/tests` nor `services/search/tests` appears in the root `pyproject.toml` `testpaths` (lines 188-189) — so nothing under either service is collected from its own tree. All coverage is centralized in `tests/unit`, and it reaches only 9 of the 39 modules in scope: `test_viewer_dataset_authz.py`, `test_viewer_object_authz.py`, `test_viewer_page_authz.py`, `test_objects_browser.py` (viewer security/datasets/objects/pages), `test_media_health_degrades.py` (viewer system), and `test_search_table_selector.py`, `test_search_similar.py`, `test_rrf_fusion.py`, `test_search_fanout.py`, `test_user_state.py` (search…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `VS-01` | E2 | **HIGH** | fastapi | S | Object-browser routes are `async def` but run blocking boto3 and a blocking Dapr secret fetch on the event loop | `services/viewer/src/viewer/api/v1/endpoints/objects.py:225`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:266` *(+6 more)* |
| `VS-02` | E2 | **HIGH** | fastapi | S | Dataset enumeration routes are `async def` and open Lance/S3 (under a threading.Lock) inline on the event loop | `services/viewer/src/viewer/api/v1/endpoints/datasets.py:64`, `services/viewer/src/viewer/api/v1/endpoints/datasets.py:76` *(+5 more)* |
| `VS-03` | E1 | **HIGH** | security | L | 25 of the viewer's 32 routes serve corpus content with no authn and no FGA gate, including every media-byte route | `services/viewer/src/viewer/api/v1/endpoints/media.py:283`, `services/viewer/src/viewer/api/v1/endpoints/media.py:161` *(+8 more)* |
| `VS-04` | E8 | **HIGH** | resources | S | Search result-cache key omits `spec.table`, so two different searchable tables of one corpus serve each other's hits | `services/search/src/search/services/result_cache.py:71`, `services/search/src/search/services/result_cache.py:94` *(+3 more)* |
| `VS-05` | E8 | **HIGH** | resources | M | `/api/page` and `/api/pages` materialize every page blob in the dataset to serve one image or one metadata page | `services/viewer/src/viewer/api/v1/endpoints/pages.py:188`, `services/viewer/src/viewer/api/v1/endpoints/pages.py:189` *(+3 more)* |
| `VS-06` | E4 | med | error-handling | M | Broad `except Exception` re-raised as `ValidationError` turns infrastructure faults into HTTP 400 client errors | `services/search/src/search/services/vector.py:57`, `services/search/src/search/services/service.py:171` *(+5 more)* |
| `VS-07` | E4 | med | error-handling | M | Five silent swallows in the search path hide real failures as empty results (three of the nine cited sites do log) | `services/search/src/search/services/service.py:298`, `services/search/src/search/services/frames.py:48` *(+7 more)* |
| `VS-08` | E8 | med | resources | M | The Cypher engine cache is a module-level mutable global holding hundreds of MB, and its lock serializes every graph request across all datasets | `services/viewer/src/viewer/api/v1/endpoints/graph.py:191`, `services/viewer/src/viewer/api/v1/endpoints/graph.py:192` *(+2 more)* |
| `VS-09` | E1 | med | security | M | `/api/media-clip` hands a `Host`-header-derived URL to ffmpeg — server-side request forgery | `services/viewer/src/viewer/api/v1/endpoints/media.py:276`, `services/viewer/src/viewer/api/v1/endpoints/media.py:279` *(+2 more)* |
| `VS-10` | E1 | med | security | S | Unsanitized S3 object key interpolated into the `Content-Disposition` header | `services/viewer/src/viewer/api/v1/endpoints/objects.py:329`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:333` |
| `VS-11` | E4 | med | error-handling | S | `_creds`' documented fail-closed handler is unreachable, so a down secret store reports "the secret is empty" | `services/viewer/src/viewer/api/v1/endpoints/objects.py:86`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:93` *(+1 more)* |
| `VS-12` | E8 | med | resilience | S | `_resolve` builds a new `httpx.Client` per catalog call while a pooled client sits on `app.state` | `services/viewer/src/viewer/api/v1/endpoints/pages.py:84` |
| `VS-13` | E1 | med | security | L | The search service has no authn/authz at all yet accepts a raw SQL `where` expression ANDed into every query | `services/search/src/search/api/v1/router.py:294`, `services/search/src/search/services/spec.py:79` *(+3 more)* |
| `VS-14` | E4 | med | error-handling | S | Rerank scores are silently misaligned with candidates when the server returns a short or sparse result list | `services/search/src/search/services/encoders/reranker.py:69`, `services/search/src/search/services/encoders/reranker.py:70` *(+1 more)* |
| `VS-15` | E8 | med | resources | M | `download_object` buffers whole objects in memory on a premise the store registry no longer guarantees | `services/viewer/src/viewer/api/v1/endpoints/objects.py:323`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:312` *(+1 more)* |
| `VS-16` | E8 | med | resilience | M | Voice similarity issues one Lance scan per hit (N+1) and a fresh ThreadPoolExecutor per encoder call | `services/viewer/src/viewer/services/voice_service.py:423`, `services/viewer/src/viewer/services/voice_service.py:425` *(+2 more)* |
| `VS-17` | E9 | med | testing | L | Neither service is in `testpaths` and neither ships tests; the retrieval core and every pure helper are uncovered | `pyproject.toml:188`, `services/viewer/src/viewer/api/v1/endpoints/media.py:78` *(+4 more)* |
| `VS-18` | E11 | med | fastapi | M | Ten routes return bare `dict[str, Any]` / `list[dict[str, Any]]`, losing response filtering and the OpenAPI contract | `services/viewer/src/viewer/api/v1/endpoints/atlas.py:81`, `services/viewer/src/viewer/api/v1/endpoints/atlas.py:218` *(+8 more)* |
| `VS-19` | E6 | med | coupling | S | Encoder construction picks its kwargs by runtime signature introspection, to accommodate test doubles | `services/search/src/search/services/clients.py:35`, `services/search/src/search/services/clients.py:45` *(+2 more)* |
| `VS-20` | E7 | low | readability | S | `parse_range` returns a three-way `tuple \| str-sentinel \| None` that the caller decodes with a rebound flag variable | `services/viewer/src/viewer/api/v1/endpoints/media.py:78`, `services/viewer/src/viewer/api/v1/endpoints/media.py:75` *(+3 more)* |
| `VS-21` | E7 | low | fastapi | S | Eleven route params use bare defaults instead of the `DatasetParam`/`Query(...)` aliases the same package defines | `services/viewer/src/viewer/api/v1/endpoints/graph.py:306`, `services/viewer/src/viewer/api/v1/endpoints/graph.py:328` *(+9 more)* |
| `VS-22` | E7 | low | readability | S | Store-name/bucket-name confusion makes the object browser's 404 name the wrong thing | `services/viewer/src/viewer/api/v1/endpoints/objects.py:159`, `services/viewer/src/viewer/api/v1/endpoints/objects.py:182` *(+3 more)* |
| `VS-23` | E11 | low | typing | S | Legacy `TypeVar` instead of PEP 695 type parameters, and a shadowed function parameter | `services/search/src/search/services/encoders/base.py:25`, `services/search/src/search/services/encoders/base.py:26` *(+2 more)* |
| `VS-24` | E5 | low | config | S | `os.getenv` read for the secret-store name outside the settings module, and a private constant imported across modules | `services/viewer/src/viewer/api/v1/endpoints/objects.py:84`, `services/viewer/src/viewer/api/v1/endpoints/voice.py:142` |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- services/viewer/src/viewer/api/v1/endpoints/objects.py:70 — `_creds` is `@lru_cache(maxsize=32)` with no TTL and no invalidation hook, so a rotated store credential is pinned for the process lifetime; the only recovery is a restart, and the docstring only reasons about NOT caching failures.
- services/viewer/src/viewer/api/v1/endpoints/graph.py:344 — `run_cypher` returns the raw exception text to the caller (`error=f"{type(exc).__name__}: {exc}"`) on an ungated route (VS-03), leaking engine/table internals from caller-supplied Cypher; it also answers HTTP 200 for a failed query, so nothing upstream can distinguish an error from an empty result.

### `maintenance`

*Layout as it actually is:* `services/maintenance` follows sanctioned layout (b), the fastapi-template shape, with a bespoke Dapr-cron HTTP surface instead of a versioned API. `src/maintenance/service.py` is the thin entrypoint: its own `FastAPI(...)` with an `@asynccontextmanager lifespan` that calls `instrument_lance_if_available()`, `assert_app_token_configured(...)`, `run_in_threadpool(apply_dapr_secrets, settings)` (fail-closed on an empty S3 secret), builds ONE `dapr.aio.DaprClient` shared by the lineage + control emitters, and stashes `lineage_emitter`, `control_emitter`, `fga_client`, `s3_client` plus `startup_complete`/`shutting_down` on `app.state`; it wires `install_problem_handlers(app, log)` (RFC 9457), `service_kit.probes` for `/livez`+`/readyz`, and `maintenance.api.routes`. Config is `core/config.py` — a single pydantic-settings `MaintenanceSettings` with `MAINTENANCE_*` aliases, `SecretStr` for the S3 key, `@lru_cache get_settings()`, and `apply_dapr_secrets()` consuming OpenBao via the Dapr secret store. DI is correct-by-the-book: `api/dependencies.py` exposes only `Annotated[T, Depends(...)]` aliases (`SettingsDep`, `LineageEmitterDep`, `FgaClientDep`, `S3ClientDep`, `ControlEmitterDep`) reading `request.app.state`. `api/routes.py` registers two POST routes + two OPTIONS acks imperatively via `router.add_api_route(f"/{binding_name}", ...)` behind `Depends(require_dapr_token)`, each…

*Tests:* Coverage is unusually good for the report/refusal logic and thin exactly where the destructive-adjacent invariants live. 12 unit files (~4,262 lines) under `tests/unit/` cover it: `test_maintenance_optimize.py` (356), `test_maintenance_policies.py` (424), `test_maintenance_gc.py`, `test_maintenance_features.py`, `test_maintenance_lineage.py` (401), `test_maintenance_sweep.py` (148), `test_orphan_files.py` (490), `test_reconcile.py` (484), `test_reconcile_report.py` (632, incl. the AST gate that keeps `reconcile.py` mutation-free), `test_reconcile_route.py` (291), `test_trash_purge.py` (665), plus `tests/e2e-py/test_maintenance_e2e.py` and `test_maintenance_s3_e2e.py`. Gaps: (1)…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `MAINT-01` | E4 | med | error-handling | S | The documented compact→cleanup→optimize order invariant is not the order the code runs (two docstrings assert a sequence the implementation contradicts) | `services/maintenance/src/maintenance/services/optimize.py:120`, `services/maintenance/src/maintenance/services/optimize.py:177` *(+4 more)* |
| `MAINT-04` | E8 | med | resilience | M | COMPLETE lineage emits are awaited one-per-dataset inside the loop and uncapped, while the FAIL emits next to them are gathered AND capped for exactly that… | `services/maintenance/src/maintenance/services/sweep.py:317`, `services/maintenance/src/maintenance/services/sweep.py:319` *(+2 more)* |
| `MAINT-05` | E4 | med | error-handling | M | Unguarded S3/Lance calls in the orphan category can raise out of reconcile() and 500 the entire tick, discarding the seven store categories that already… | `services/maintenance/src/maintenance/services/reconcile.py:653`, `services/maintenance/src/maintenance/services/reconcile.py:667` *(+4 more)* |
| `MAINT-06` | E4 | med | error-handling | S | The branch / MemWAL layout gate swallows its probe error and continues, failing OPEN on the one check that exists to stop live files being reported as garbage | `services/maintenance/src/maintenance/services/orphans.py:275`, `services/maintenance/src/maintenance/services/orphans.py:278` *(+1 more)* |
| `MAINT-07` | E2 | med | resilience | S | reconcile() builds a boto3 S3 client per call inside an async def when the lifespan-built client is absent — blocking construction on the event loop, and it… | `services/maintenance/src/maintenance/services/reconcile.py:704`, `services/maintenance/src/maintenance/services/reconcile.py:710` *(+2 more)* |
| `MAINT-08` | E4 | med | config | S | reconcile()'s control_root default falls back to the POLICY root, not the control root — the exact root-confusion the sweep documents as a… | `services/maintenance/src/maintenance/services/reconcile.py:701`, `services/maintenance/src/maintenance/services/reconcile.py:693` *(+2 more)* |
| `MAINT-09` | E7 | med | readability | L | run_sweep (172 lines) and compact_one (153 lines) are god-functions with 5-level nesting mixing discovery, registry reads, policy resolution, tracing, IO and… | `services/maintenance/src/maintenance/services/sweep.py:94`, `services/maintenance/src/maintenance/services/sweep.py:179` *(+4 more)* |
| `MAINT-10` | E6 | med | duplication | M | build_report repeats the same guard/else block seven times, and two categories pass the same reason pair to _first in opposite order | `services/maintenance/src/maintenance/services/reconcile.py:550`, `services/maintenance/src/maintenance/services/reconcile.py:556` *(+5 more)* |
| `MAINT-11` | E12 | med | dead-code | S | core/lance_trace.py is a 217-line module with zero callers and zero tests, plus an unused constant in orphans.py | `services/maintenance/src/maintenance/core/lance_trace.py:183`, `services/maintenance/src/maintenance/core/lance_trace.py:68` *(+2 more)* |
| `MAINT-12` | E8 | med | resources | M | The multi-base layout gate issues one sequential S3 HEAD per referenced path with no batching and no short-circuit, on every dataset in every bucket | `services/maintenance/src/maintenance/services/orphans.py:283`, `services/maintenance/src/maintenance/services/orphans.py:286` |
| `MAINT-03` | E8 | low | resilience | S | reconcile's load_sources awaits six independent stores sequentially where one asyncio.gather would do | `services/maintenance/src/maintenance/services/reconcile.py:511`, `services/maintenance/src/maintenance/services/reconcile.py:513` *(+4 more)* |
| `MAINT-13` | E7 | low | structure | M | Routes are registered at import time from a module-level get_settings(), with tags on each route instead of the APIRouter | `services/maintenance/src/maintenance/api/routes.py:157`, `services/maintenance/src/maintenance/api/routes.py:159` *(+3 more)* |
| `MAINT-14` | E1 | low | security | S | docs_enabled defaults to True and the chart never sets it false, so /docs and /openapi.json are served in production despite the comment claiming otherwise | `services/maintenance/src/maintenance/core/config.py:32`, `services/maintenance/src/maintenance/service.py:163` *(+1 more)* |
| `MAINT-15` | E10 | low | observability | S | Identity drift left over from the compaction→maintenance rename: a wire-visible OpenLineage producer URI pointing at a path and repo that no longer exist, and… | `services/maintenance/src/maintenance/core/lineage_emit.py:53`, `services/maintenance/src/maintenance/core/metrics.py:8` *(+2 more)* |
| `MAINT-16` | E7 | low | readability | M | Helpers with 4-5 positional parameters and functions that mutate a caller-owned report/output argument in place | `services/maintenance/src/maintenance/services/sweep.py:54`, `services/maintenance/src/maintenance/services/optimize.py:104` *(+6 more)* |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- services/maintenance/src/maintenance/core/config.py:74 — same identity drift as MAINT-15 but WIRE-VISIBLE on the lineage graph: `lineage_job_namespace: str = Field(default="compaction", ...)` and lineage_emit.py:50 `COMPACTION = "compaction"` mean every emitted event's job is `{namespace: "compaction", name: "compaction.<table_id>"}` for a service named `maintenance`, persisted into AGE forever.
- services/maintenance/src/maintenance/services/orphans.py:337 — the sidecar-prefix test `any(rel.startswith(d) for d in referenced if d.endswith("/"))` re-filters and re-scans the ENTIRE referenced set for every non-matching listed file, i.e. O(files x referenced) Python-level work per dataset; the trailing-slash subset should be materialized once outside the loop (same HOUSE-RULE-16 class as MAINT-12).
- services/maintenance/src/maintenance/services/optimize.py:245 — the auto_cleanup failure path overwrites `result.error` with `f"auto_cleanup: {exc}"`, which sweep.py:312 then classifies (it does not start with `maintain:`) as "no event", so a dataset whose version reclamation could not be configured emits no FAIL lineage AND is counted in summarize()'s `errors` — the two failure surfaces disagree about what happened.

### `flows-fleet`

*Layout as it actually is:* Four services, three shapes. **gateway** is a single 346-line module (`services/gateway/src/gateway/__init__.py`) — bespoke by ruling: its own `FastAPI(...)`, its own `@asynccontextmanager lifespan` building one `httpx.AsyncClient` and a route table onto `app.state`, one `@app.middleware("http")` (the lineage sidecar-only 403 blocklist), an unproxied `/healthz`, and one `@app.api_route("/api/{path:path}", methods=[7 verbs])` catch-all that longest-prefix-matches a table of positional 4-tuples and re-streams the upstream with `StreamingResponse` + `BackgroundTask(aclose)`. It has no Settings class, no `service_kit` exception handlers and no DI deps — all config is 15 inline `os.environ.get` calls, several evaluated per request. **compute** is the reference fleet flat-module + `make_service_app` build: `health`/`routes` mounted under `api_prefix`, a root-mounted composite of `proxy` (read-only Ray Serve reverse proxy) + `pruner` (Dapr cron binding), `dependencies.py` of `Annotated[X, Depends(getter)]` aliases, and `make_lifespan(settings)` putting `http` + `ray_client` on `app.state`. **controlplane** is the same factory with the *default* (resource-free) lifespan: `k8s.py` holds a `ProjectReader` Protocol plus the live client, `service.py` is pure CR→DTO mapping, `routes.py` is one `def` route behind an `@lru_cache` reader dependency. **flows** is the factory plus its own…

*Tests:* **gateway** — 6 test files (~520 lines): the route table and its ordering, the Location rewrite, the client-spoofable-header strip (including a test that pins FastAPI's *first*-duplicate header binding, the framework fact the whole defence rests on), the lineage 403 guard across trailing-slash/case/dot-segment variants, and two excellent "the rewrite lands on a path the service ACTUALLY serves" tests driven off the real `flows`/`ingest` openapi. Thin: nothing exercises percent-encoded path segments (finding GW-URL-DECODE), the 502 branch, `_merged_openapi`, or `/healthz`. **flows** — the strongest suite in scope: the pure graph layer, per-node dispatch mocked at the transport with respx,…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `FLOWS-NODE-ESCAPE` | E4 | **HIGH** | error-handling | S | `run_node` catches only `NodeError`, so a bad `regexReplace` 500s the entire run and orphans its sibling nodes | `services/flows/src/flows/executor.py:273`, `services/flows/src/flows/executor.py:249` *(+2 more)* |
| `FLOWS-REDOS-ON-LOOP` | E1 | **HIGH** | security | M | Caller-supplied regex is compiled and executed on the event loop — a `regex` node freezes the whole flows process | `services/flows/src/flows/executor.py:247`, `services/flows/src/flows/executor.py:253` *(+4 more)* |
| `GW-URL-DECODE` | E4 | **HIGH** | error-handling | M | Gateway rebuilds the upstream URL from the percent-DECODED path, corrupting or truncating it (and 500-ing on some inputs) | `services/gateway/src/gateway/__init__.py:315`, `services/gateway/src/gateway/__init__.py:324` *(+2 more)* |
| `COMPUTE-PROXY-CONTENT-ENCODING` | E4 | med | error-handling | S | The Serve proxy forwards `content-encoding`/`content-length` from an upstream body httpx has already decompressed | `services/compute/src/compute/proxy.py:45`, `packages/ray-kit/src/ray_kit/dashboard.py:582` *(+1 more)* |
| `COMPUTE-RAY-CLIENT-RETRY-STORM` | E3 | med | resilience | S | `get_ray_client` re-runs the blocking `build_client` on every request while Ray is down, with no backoff or negative caching | `services/compute/src/compute/dependencies.py:21`, `services/compute/src/compute/lifespan.py:29` |
| `CP-503-NEVER-FIRES` | E4 | med | error-handling | S | The controlplane's designed 503 cannot fire for its likeliest cause — `load_kube_config()` raises inside the dependency, outside the route's try | `services/controlplane/src/controlplane/routes.py:25`, `services/controlplane/src/controlplane/routes.py:39` *(+1 more)* |
| `CP-INGRESS-N-PLUS-ONE` | E8 | med | resilience | S | One blocking Kubernetes API call per project to resolve ingress hosts, where one cluster-wide call would do | `services/controlplane/src/controlplane/service.py:42`, `services/controlplane/src/controlplane/k8s.py:40` |
| `FLEET-ENV-SCATTER` | E5 | med | config | M | 19 raw `os.environ` reads outside any settings module — the gateway has no Settings class at all, and several reads happen per request | `services/gateway/src/gateway/__init__.py:109`, `services/gateway/src/gateway/__init__.py:114` *(+8 more)* |
| `FLOWS-BROAD-EXCEPT` | E4 | med | error-handling | S | Two unbounded `except Exception` blocks make a code defect indistinguishable from an absent sidecar | `services/flows/src/flows/lifespan.py:61`, `services/flows/src/flows/routes.py:98` *(+1 more)* |
| `FLOWS-DURABLE-RUN-UNREADABLE` | E3 | med | dapr-events | M | A durable run reports `running` forever — the workflow's terminal state is never read back into `GET /flows/runs/{id}` | `services/flows/src/flows/routes.py:113`, `services/flows/src/flows/routes.py:130` *(+2 more)* |
| `GW-502-LEAKS-INTERNAL-ADDRESS` | E1 | med | security | S | The 502 body prints the internal upstream address to the public caller — the exact leak `_rewrite_location` exists to prevent | `services/gateway/src/gateway/__init__.py:333`, `services/gateway/src/gateway/__init__.py:80` |
| `GW-BUFFERS-REQUEST-BODY` | E8 | med | resources | M | Every proxied request body is fully buffered in memory, though responses are correctly streamed | `services/gateway/src/gateway/__init__.py:327`, `services/gateway/src/gateway/__init__.py:341` |
| `GW-NO-PROBLEM-JSON` | E4 | med | error-handling | S | The gateway is the only service in the fleet that does not answer RFC 9457 problem+json | `services/gateway/src/gateway/__init__.py:266`, `services/gateway/src/gateway/__init__.py:318` *(+2 more)* |
| `GW-OPENAPI-SEQUENTIAL` | E8 | med | resilience | S | `/openapi.json` fetches ten backends sequentially with a 10 s timeout each — worst case ~100 s on a client-facing request | `services/gateway/src/gateway/__init__.py:238`, `services/gateway/src/gateway/__init__.py:241` *(+1 more)* |
| `COMPUTE-UNBOUNDED-QUERY-PARAMS` | E7 | low | fastapi | S | `lines` is forwarded unbounded to the Ray dashboard; both it and `tail` are declared without `Annotated[..., Query(...)]` | `services/compute/src/compute/routes.py:35`, `services/compute/src/compute/routes.py:66` |
| `CP-CR-UNVALIDATED` | E11 | low | typing | M | Kubernetes CRs are walked as `dict[str, Any]` with `.get()` chains instead of validated at the boundary | `services/controlplane/src/controlplane/service.py:24`, `services/controlplane/src/controlplane/service.py:32` *(+1 more)* |
| `FLOWS-422-BYPASSES-HIERARCHY` | E4 | low | error-handling | M | `create_run` hand-builds a problem+json body and returns `RunState \| JSONResponse` because the shared exception hierarchy cannot carry extension members | `services/flows/src/flows/routes.py:66`, `services/flows/src/flows/routes.py:80` *(+2 more)* |
| `GW-OPENAPI-SILENT-SHADOW` | E10 | low | observability | S | The merged OpenAPI silently drops colliding paths — every service's `/api/health` overwrites the previous one | `services/gateway/src/gateway/__init__.py:248`, `services/gateway/src/gateway/__init__.py:249` |
| `GW-ROUTE-TUPLE` | E7 | low | typing | S | Route rows are positional 4-tuples read by index, and `_merged_openapi` returns a bare `dict` | `services/gateway/src/gateway/__init__.py:94`, `services/gateway/src/gateway/__init__.py:170` *(+3 more)* |
| `TEST-CONFTEST-ENV-LEAK` | E9 | low | testing | S | `compute/tests/conftest.py` mutates `os.environ` at import, leaking into every other suite in the session | `services/compute/tests/conftest.py:12`, `services/compute/tests/conftest.py:13` *(+1 more)* |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- services/gateway/src/gateway/__init__.py:241 — _merged_openapi fetches f"{base}{prefix}/openapi.json" for EVERY target, ignoring each row's upstream prefix; catalog and lineage serve openapi at bare /openapi.json (catalog/src/catalog/main.py:184, lineage/src/lineage/main.py:128) and their route rows rewrite the public prefix to "", so those fetches 404 and the whole lance plane is silently absent from the 'unified' spec — same silent-drop class as GW-OPENAPI-SILENT-SHADOW, one line above it.
- services/gateway/src/gateway/__init__.py:266 — the gateway never calls service_kit.middleware.register_middleware either (only setup_otel), so the front door — the one origin the browser talks to — is the sole app in the fleet with no shared CORS/middleware stack; same root cause as GW-NO-PROBLEM-JSON (bypassing make_service_app) but a distinct missing call.
- packages/ray-kit/src/ray_kit/dashboard.py:519 — `if len(lines) > tail: lines = lines[-tail:]` inverts for tail<=0: `lines[-0:]` returns the full log, so ?tail=0 returns everything the cap exists to prevent.

### `service-kit-core`

*Layout as it actually is:* `packages/service-kit` is the platform library, and its package `__init__.py` IS the app factory (not a thin re-export): `make_service_app(*, title, routers, proxy_router, lifespan)` calls `_setup_logging()`, `build_settings()` (which runs `load_dotenv()` + `storage.derive_hcp_creds()` and validates `Settings`), wraps the caller-supplied `LifespanFactory` in a second `@asynccontextmanager` that builds/closes the Dapr client on `app.state.dapr`, constructs `FastAPI(...)` with docs mounted under `settings.api_prefix`, then `register_handlers` (DomainError + RequestValidationError → RFC 9457 problem+json), `register_middleware` (CORS if configured → RequestID → Timing, all `BaseHTTPMiddleware`), mounts routers under the prefix, disables `redirect_slashes` and adds the pure-ASGI `SlashToleranceMiddleware`, and finally `setup_otel` (lazy OTel SDK imports, opt-in via `RASK_OTEL_ENABLED` or `OTEL_EXPORTER_OTLP_ENDPOINT`). Config is `pydantic-settings` with `RASK_*` aliases (`service_kit/config.py`), injected as `SettingsDep = Annotated[Settings, Depends(get_settings)]` reading `request.app.state.settings`. Alongside that base sit three subpackages I also covered: `schemas/` (pure Pydantic v2 response models, dependency-free), `media/` (a SECOND, parallel app skeleton with its own `Settings` on `MEDIA_*` env vars, its own `@lru_cache get_settings`, its own CORS-only…

*Tests:* Seven test files under `packages/service-kit/tests/` (in root `testpaths`), covering roughly five of the ~40 in-scope modules: `test_slash_tolerance.py` (5 tests, the ASGI slash middleware end-to-end through `make_service_app`), `test_otel.py` (2, `setup_otel` on/off), `test_dapr.py` (4 + a `governed.dapr_auth` case, settings gating + the client factory address), `test_commit_conflict.py` (4, `writer.translate_commit_conflict` in both directions), `test_predicate_or.py` (5, `predicate.or_` grouping), plus `test_fga_provision.py` / `test_media_s3_secret.py` (governed/media secret paths). Repo-root `tests/unit/` adds indirect coverage: `test_openlineage_spec_conformance.py` pins…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `SK-01` | E1 | **HIGH** | security | S | Write authorship comes from an unverified, client-supplied `X-User` header | `packages/service-kit/src/service_kit/media/deps.py:29`, `services/annotator/src/annotator/annotations/save.py:58` *(+1 more)* |
| `SK-02` | E4 | med | error-handling | S | `LocalCatalogWriteTransport` (dev/offline catalog fallback) omits the commit-conflict translation the direct path has — 500 instead of 409 | `packages/service-kit/src/service_kit/lancekit/writer.py:141`, `packages/service-kit/src/service_kit/lancekit/writer.py:144` *(+3 more)* |
| `SK-03` | E8 | med | resources | M | A fresh urllib3 `ApiClient` is constructed per catalog read/write call and never closed | `packages/service-kit/src/service_kit/lancekit/reader.py:348`, `packages/service-kit/src/service_kit/lancekit/reader.py:443` *(+8 more)* |
| `SK-04` | E4 | med | error-handling | S | Errors are classified by substring-matching exception messages in three separate places | `packages/service-kit/src/service_kit/lancekit/registry.py:157`, `packages/service-kit/src/service_kit/lancekit/reader.py:262` *(+1 more)* |
| `SK-05` | E4 | med | error-handling | M | A transient per-table read failure is laundered into a 404 "dataset not found" | `packages/service-kit/src/service_kit/lancekit/introspect.py:97`, `packages/service-kit/src/service_kit/lancekit/descriptor.py:249` *(+2 more)* |
| `SK-06` | E8 | med | resources | M | The dataset registry is lazily built inside request handling, unguarded, and the handles are never released | `packages/service-kit/src/service_kit/media/state.py:78`, `packages/service-kit/src/service_kit/media/state.py:50` *(+2 more)* |
| `SK-07` | E10 | med | observability | S | `configure_app_logging`'s logger allow-list has drifted from the fleet — `maintenance` opts in and gets nothing | `packages/service-kit/src/service_kit/obs.py:25`, `packages/service-kit/src/service_kit/obs.py:30` *(+2 more)* |
| `SK-08` | E5 | med | config | M | `make_service_app` reads `.env` and builds `Settings` at import time; settings are not injectable | `packages/service-kit/src/service_kit/__init__.py:104`, `packages/service-kit/src/service_kit/__init__.py:46` *(+4 more)* |
| `SK-09` | E7 | med | structure | M | Lifecycle invariants the shared probes depend on are unenforced convention, hand-rolled in every service | `packages/service-kit/src/service_kit/probes.py:56`, `packages/service-kit/src/service_kit/probes.py:58` *(+8 more)* |
| `SK-12` | E8 | med | resources | M | `lancekit.store` builds a new `pyarrow.fs.S3FileSystem` per call and bypasses `packages/storage` | `packages/service-kit/src/service_kit/lancekit/store.py:27`, `packages/service-kit/src/service_kit/lancekit/store.py:51` *(+3 more)* |
| `SK-13` | E2 | med | readability | S | `Settings.storage_options` is a property that performs a blocking Dapr secret fetch and raises | `packages/service-kit/src/service_kit/media/config.py:167`, `packages/service-kit/src/service_kit/media/config.py:194` *(+2 more)* |
| `SK-10` | E5 | low | config | M | Name collision inside one package: two `Settings` classes and two `get_settings` with different DI shapes (`RASK_*` vs `MEDIA_*`) | `packages/service-kit/src/service_kit/config.py:17`, `packages/service-kit/src/service_kit/media/config.py:21` *(+3 more)* |
| `SK-11` | E6 | low | duplication | M | Duplicate column-lineage builders + a same-named 3-tuple `ColumnEdge`, and stale TRANSITIONAL markers pointing at a gate whose target package now exists | `packages/service-kit/src/service_kit/openlineage.py:115`, `packages/service-kit/src/service_kit/openlineage.py:118` *(+4 more)* |
| `SK-14` | E5 | low | config | S | `RASK_*` env vars read directly via `os.environ` outside the settings modules | `packages/service-kit/src/service_kit/__init__.py:33`, `packages/service-kit/src/service_kit/schemas/storage.py:157` |
| `SK-15` | E12 | low | dead-code | S | `_setup_logging` configures logger trees no package in the repo produces — `RASK_LOG_LEVEL` is inert | `packages/service-kit/src/service_kit/__init__.py:31`, `packages/service-kit/src/service_kit/__init__.py:34` *(+1 more)* |
| `SK-16` | E10 | low | observability | S | Lineage emitter logs under a hardcoded `"lineage"` logger and writes events straight to stdout | `packages/service-kit/src/service_kit/lancekit/lineage_emit.py:27`, `packages/service-kit/src/service_kit/lancekit/lineage_emit.py:89` *(+1 more)* |
| `SK-17` | E7 | low | structure | M | No `__all__` and no public/private marking: importing anything from `service_kit` executes the app factory module | `packages/service-kit/src/service_kit/__init__.py:1`, `packages/service-kit/src/service_kit/schemas/__init__.py:1` *(+3 more)* |
| `SK-18` | E5 | low | typing | S | Constrained settings validated by hand-rolled `field_validator`s instead of `StrEnum`/`Literal` | `packages/service-kit/src/service_kit/media/config.py:115`, `packages/service-kit/src/service_kit/media/config.py:122` *(+2 more)* |
| `SK-19` | E11 | low | typing | S | `dataset_handle` (the shared media resolution entry point, ~34 callers) has no return annotation — inside an explicitly ANN-exempt tree | `packages/service-kit/src/service_kit/media/state.py:68`, `packages/service-kit/src/service_kit/slash.py:18` *(+3 more)* |
| `SK-20` | E4 | low | error-handling | S | Bare `except Exception` swallow-and-continue in three shared code paths | `packages/service-kit/src/service_kit/lancekit/introspect.py:97`, `packages/service-kit/src/service_kit/lancekit/registry.py:79` *(+1 more)* |
| `SK-21` | E12 | low | dead-code | S | Stale references to deleted packages, migrated gates and a renamed frontend layout | `packages/service-kit/src/service_kit/openlineage.py:17`, `packages/service-kit/src/service_kit/lancekit/openlineage.py:15` *(+6 more)* |
| `SK-22` | E7 | low | fastapi | S | CORS is registered first and therefore ends up the INNERMOST of five middleware layers | `packages/service-kit/src/service_kit/middleware.py:7`, `packages/service-kit/src/service_kit/middleware.py:66` *(+2 more)* |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- packages/service-kit/src/service_kit/media/deps.py:20 — `get_state` returns `request.app.state.resources` with no guard or fallback; a lifespan that forgets to set `resources` turns every media route into a 500 AttributeError. Same unenforced lifespan-contract class as SK-09 (startup_complete / app.state.settings), and a third site the factory could own.
- packages/service-kit/src/service_kit/media/state.py:42 — `settings: Settings = Field(default_factory=get_settings)` binds AppState to the `@lru_cache`d process-global `media.config.get_settings()`, so two app instances (notably tests) silently share one settings object — and it contradicts the request-scoped `service_kit.dependencies.get_settings` the same package exposes (SK-10's collision, with a concrete consequence).
- services/viewer/src/viewer/api/v1/endpoints/datasets.py:49 — `_registry(state)` is a verbatim second copy of `dataset_handle`'s unguarded lazy `DatasetRegistry` construction (SK-06), so the race and the missing lifespan ownership exist at two sites, not one; any fix must remove both.

### `service-kit-governed`

*Layout as it actually is:* Two framework-light subpackages of the shared `service-kit` platform library; neither owns a FastAPI app, so neither of the two sanctioned service layouts applies — both are libraries consumed by the 7 lance services plus ingest. `governed/` is the authn/authz/audit kernel: `fga.py` (1237 lines, a flat module-level function library over `openfga_sdk` — every operation takes an injected `OpenFgaClient` plus the same three-field retry triple, wraps its SDK call in a per-call tenacity closure via `_retrying`, and fails closed into `lance_namespace.ServiceUnavailableError`); `oidc.py` (a stateful `OIDCVerifier` with a per-issuer discovery/JWKS TTL cache over a sync `httpx.Client`, PyJWT-only, local algorithm allowlist); `deps.py` (a `make_auth_deps(settings_dep)` FACTORY — deliberately no `from __future__ import annotations` — producing sync FastAPI deps plus an `FgaChecker` Protocol whose FGA-off branch is permissive by construction, FGA-on-without-client is 503); `dapr_auth.py` (sidecar-token + public-front-door refusal deps, reading `os.environ` directly on the request path); `secrets.py` (sync Dapr secret-store fetch, fail-closed via `fetch_required_secrets`); `user_state.py` (async `UserStateStore` over an injected/owned `httpx.AsyncClient` with an `aclose`); `audit.py` (one dedicated `lance.audit` logger); `settings.py` (a plain non-BaseSettings `GovernedAuthSettings` mixin…

*Tests:* In-package tests barely touch this scope: of the 7 suites in `packages/service-kit/tests`, only `test_fga_provision.py` (one test — `provision` must write the `conditions` block) exercises it; the rest cover `media`, `lancekit`, `dapr_publish` and `otel`. The real coverage lives in the root `tests/unit` testpath and is substantial: `test_fga_resilience.py` (13 tests — transient classification, fail-closed on every read path, subject qualification, the batch-duplicate single-write fallback), `test_fga_expand.py` (28 tests including the whole `expand_tree` walk, depth clamping, qualified-userset normalisation, `read_changes`), `test_fga_revoke.py` (10), `test_ns_errors_contract.py` (5, and…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `SKG-01` | E4 | **HIGH** | error-handling | M | Idempotency of tuple writes/deletes is decided by substring-matching the OpenFGA error body, so a genuinely rejected write or revoke is swallowed AND audited… | `packages/service-kit/src/service_kit/governed/fga.py:105`, `packages/service-kit/src/service_kit/governed/fga.py:109` *(+4 more)* |
| `SKG-02` | E1 | med | fga | S | Partial batch failure in write_tuples/delete_tuples skips the audit trail entirely for the tuples that DID land | `packages/service-kit/src/service_kit/governed/fga.py:1058`, `packages/service-kit/src/service_kit/governed/fga.py:1084` *(+2 more)* |
| `SKG-03` | E1 | med | fga | M | expand_tree reports every repeated object#relation as `cycle: True`, so an ordinary concentric diamond is mislabelled a loop and its subtree is dropped | `packages/service-kit/src/service_kit/governed/fga.py:725`, `packages/service-kit/src/service_kit/governed/fga.py:727` *(+1 more)* |
| `SKG-04` | E4 | med | resilience | M | Two silent-truncation paths surface only to the log; the return value is indistinguishable from a complete answer | `packages/service-kit/src/service_kit/governed/fga.py:547`, `packages/service-kit/src/service_kit/governed/fga.py:905` *(+1 more)* |
| `SKG-05` | E4 | med | error-handling | M | governed/deps.py raises the FLEET exception taxonomy while its three sibling modules raise the Lance one — a latent off-contract 401/503 for any lance-plane… | `packages/service-kit/src/service_kit/governed/deps.py:38`, `packages/service-kit/src/service_kit/governed/deps.py:87` *(+5 more)* |
| `SKG-06` | E6 | med | duplication | L | The three-field retry triple and the fail-closed except block are copy-pasted through all thirteen FGA operations | `packages/service-kit/src/service_kit/governed/fga.py:361`, `packages/service-kit/src/service_kit/governed/fga.py:419` *(+11 more)* |
| `SKG-07` | E8 | med | resources | S | make_client returns an aiohttp-backed OpenFgaClient with no disposal contract, and seven of its nine fleet call sites never close it (only lineage and the… | `packages/service-kit/src/service_kit/governed/fga.py:318`, `packages/service-kit/src/service_kit/governed/fga.py:336` *(+1 more)* |
| `SKG-08` | E6 | med | duplication | L | Four hand-rolled object-store record registries with byte-identical hashed-key helpers and identical list-with-broad-except bodies | `packages/service-kit/src/service_kit/lakehouse/protection.py:40`, `packages/service-kit/src/service_kit/lakehouse/maintenance_policies.py:52` *(+5 more)* |
| `SKG-09` | E11 | med | typing | L | Every lakehouse control-plane record is an unvalidated dict[str, Any]; write paths index required keys with no boundary validation | `packages/service-kit/src/service_kit/lakehouse/protection.py:46`, `packages/service-kit/src/service_kit/lakehouse/protection.py:55` *(+7 more)* |
| `SKG-10` | E5 | med | config | M | Five direct os.environ reads outside any Settings class, three on the per-request path, plus an unprefixed env var and a hard-coded Dapr sidecar port | `packages/service-kit/src/service_kit/governed/dapr_auth.py:45`, `packages/service-kit/src/service_kit/governed/dapr_auth.py:96` *(+6 more)* |
| `SKG-11` | E8 | med | resources | M | Module-level mutable cache global in warehouse_registry with no bound and no eviction | `packages/service-kit/src/service_kit/lakehouse/warehouse_registry.py:49`, `packages/service-kit/src/service_kit/lakehouse/warehouse_registry.py:117` *(+2 more)* |
| `SKG-12` | E8 | med | resilience | S | The JWKS fetch has no explicit timeout while the discovery fetch beside it does | `packages/service-kit/src/service_kit/governed/oidc.py:207`, `packages/service-kit/src/service_kit/governed/oidc.py:186` *(+1 more)* |
| `SKG-13` | E6 | med | duplication | M | lakehouse.sources.S3Source / sinks.S3Sink shadow the canonical storage.S3Source / storage.S3Sink by name with an incompatible API | `packages/service-kit/src/service_kit/lakehouse/sources.py:74`, `packages/service-kit/src/service_kit/lakehouse/sinks.py:36` *(+3 more)* |
| `SKG-14` | E9 | med | testing | M | The entire audited scope sits under a blanket 21-rule ruff exemption declared temporary for verbatim-copied files that have since been rewritten in place | `pyproject.toml:109`, `pyproject.toml:110` *(+2 more)* |
| `SKG-15` | E4 | low | error-handling | S | fetch_dapr_secret swallows every exception into an empty bundle and logs a boot-blocking failure at WARNING | `packages/service-kit/src/service_kit/governed/secrets.py:74`, `packages/service-kit/src/service_kit/governed/secrets.py:77` |
| `SKG-16` | E11 | low | typing | S | Any/object on public signatures where a Protocol or Callable alias exists, and a decorator factory with no return annotation | `packages/service-kit/src/service_kit/governed/deps.py:66`, `packages/service-kit/src/service_kit/governed/deps.py:72` *(+2 more)* |
| `SKG-17` | E6 | low | duplication | S | S3Source re-implements its own listing-and-sort inline instead of calling the _listing helper it already has | `packages/service-kit/src/service_kit/lakehouse/sources.py:86`, `packages/service-kit/src/service_kit/lakehouse/sources.py:99` |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- packages/service-kit/src/service_kit/lakehouse/maintenance_policies.py:75 — `get_policy` returns `json.loads(stream.readall().decode())` with no `isinstance(loaded, dict)` guard, while both its twins (protection.get_protection:63-67 and trash.get:82-87) do check and warn; a non-dict or malformed policy record propagates straight into `resolve_policy`. Same missing-boundary-validation rule as SKG-09, and an unflagged instance of the SKG-08 drift.
- packages/service-kit/src/service_kit/lakehouse/trash.py:110 — the `# noqa: BLE001` here (and the identical one at warehouse_records.py:47) is a DEAD directive: `BLE` is not in pyproject's `[tool.ruff.lint] select` list, so no rule is being suppressed, and RUF100 (unused-noqa) would normally catch it but is itself in the lakehouse/** exemption. The comment asserts a suppression rationale that does nothing — direct evidence for SKG-14.
- packages/service-kit/src/service_kit/governed/fga.py:1006 — `_audit_tuples` hard-codes `SUCCESS` and there is no failure counterpart anywhere in the write path, so the library's structural-coverage claim (:1015) holds only for successes; every fail-closed `raise ServiceUnavailableError` at :1074/:1091/:1191 leaves no audit row at all, even though the catalog's endpoint-level audit (access_admin.py:264, asserted in tests/unit/test_access_admin.py:225) does emit a `failure` outcome.

### `packages-small`

*Layout as it actually is:* Five library-only uv workspace members, no entrypoints anywhere (correct for this layer — no CLI, no FastAPI app, no `if __name__`). **storage** (`src/storage/`, 7 modules) is the S3/FS boundary: `client.s3_client` builds the one boto3 client from hand-rolled `os.getenv` tuples (`_ENDPOINT_ENVS`/`_INSECURE_ENVS`/`_CA_BUNDLE_ENVS`) rather than pydantic-settings; `s3.py`/`fs.py` hold picklable Source/Sink pairs (duck-typed, no `Protocol`); `iiif.py` adds a read-through IIIF cache; `errors.py` is a newer backend-neutral exception taxonomy (`s3_errors` contextmanager) that the package's own read/write paths do not yet use. **tracker** (`src/tracker/`, 6 modules) is a SQLModel/SQLAlchemy buffered upsert tracker with a `_BufferedSqlTracker` base + `SqliteTracker`/`PostgresTracker` subclasses selected by `factory.create_tracker`, guarded by a `runtime_checkable` `TrackerProtocol`; it has zero consumers in the repo. **validate** (`src/validate/`, 2 modules) is pure functions: `images.py` (bytes validators + a dict-dispatch extension map — clean) and `rules.py` (a `@dataclass Rule` + closure-returning rule factories, entirely unconsumed). **lineage-kit** (`src/lineage_kit/`, 8 modules) is the best-shaped member: `config.LineageSettings` (pydantic-settings, `RASK_LINEAGE_` prefix + `AliasChoices`), an `Emitter` `Protocol` with Noop/Recording/Client implementations, a `LineageRun` state…

*Tests:* Coverage is very uneven across the group. **lineage-kit is exemplary** — 6 suites (config/noop/transitions/linkage/consume/spec) with an autouse env+emitter isolation fixture, a `RecordingEmitter` seam, spec-URL equality gates against the installed `openlineage-python`, async-stage terminal ordering, cross-process context rehydration, and the half-configured service-door case pinned with its rationale. **ray-kit** covers the two things that already burned production (job-listing bounds/OOM in `test_dashboard_bounds.py`, retention safety in `test_prune_jobs.py`) plus token-auth wiring, but `submit.py` has **no tests at all** in the package (`submission_id`, `submit_or_reattach`'s reattach…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `PS-01` | E3 | med | resilience | S | `client.meta.events.unregister("needs-retry.s3")` is a proven no-op — the retry handler it means to remove is still registered | `packages/storage/src/storage/client.py:102`, `packages/storage/src/storage/client.py:81` |
| `PS-02` | E4 | med | error-handling | M | storage's own error taxonomy is half-applied: `s3_errors` wraps nothing inside `packages/storage`, and `iiif.py` re-implements it inline | `packages/storage/src/storage/iiif.py:197`, `packages/storage/src/storage/iiif.py:201` *(+4 more)* |
| `PS-03` | E3 | med | resilience | S | Hand-rolled retry loop with `time.sleep` and a bare `assert` in `fetch_image` | `packages/storage/src/storage/iiif.py:87`, `packages/storage/src/storage/iiif.py:90` *(+2 more)* |
| `PS-04` | E6 | med | duplication | M | Three copies of the lazy-client/pickle dance and four copies of the `list_objects_v2` paginate loop, with no `Source`/`Sink` Protocol to anchor them | `packages/storage/src/storage/s3.py:49`, `packages/storage/src/storage/s3.py:100` *(+7 more)* |
| `PS-05` | E5 | med | config | S | `derive_hcp_creds` mutates process-global `os.environ` to inject S3 credentials from env secrets | `packages/storage/src/storage/client.py:43`, `packages/storage/src/storage/client.py:48` *(+1 more)* |
| `PS-06` | E5 | med | config | S | `packages/storage` declares `requires-python = ">=3.10"` but uses PEP 695 `type` syntax (3.12+) — the metadata is factually wrong | `packages/storage/pyproject.toml:5`, `packages/storage/src/storage/client.py:18` *(+1 more)* |
| `PS-09` | E12 | med | dead-code | S | `packages/tracker` has zero consumers and reintroduces the relational store P7a removed — while pulling sqlmodel + psycopg into the root lock | `packages/tracker/src/tracker/__init__.py:1`, `pyproject.toml:60` *(+2 more)* |
| `PS-10` | E7 | med | structure | M | tracker's backend-agnostic contract is broken: the schema migration is SQLite-only, and DDL runs in the constructor with no migration path | `packages/tracker/src/tracker/_base.py:70`, `packages/tracker/src/tracker/sqlite.py:49` *(+3 more)* |
| `PS-13` | E12 | med | dead-code | S | `validate/rules.py` is entirely unconsumed — 5 exported symbols, 0 callers, 0 tests | `packages/validate/src/validate/rules.py:20`, `packages/validate/src/validate/rules.py:34` *(+4 more)* |
| `PS-15` | E9 | med | testing | S | `ray_kit.submit` — deterministic ids and the reattach-or-resubmit branch — has no tests, despite its own docstring naming it the hard part | `packages/ray-kit/src/ray_kit/submit.py:86`, `packages/ray-kit/src/ray_kit/submit.py:53` *(+1 more)* |
| `PS-16` | E8 | med | resilience | S | Independent dashboard GETs are awaited sequentially in three functions | `packages/ray-kit/src/ray_kit/dashboard.py:281`, `packages/ray-kit/src/ray_kit/dashboard.py:295` *(+4 more)* |
| `PS-17` | E4 | med | error-handling | S | `logs()` reports `ok=True, "(empty or unavailable)"` for every status ≥ 400 — a 401 from a token-authed dashboard renders as an empty log file | `packages/ray-kit/src/ray_kit/dashboard.py:538`, `packages/ray-kit/src/ray_kit/dashboard.py:539` *(+1 more)* |
| `PS-18` | E4 | med | error-handling | S | `job_logs` catches bare `Exception` while its six siblings catch `RAY_TRANSIENT_ERRORS` | `packages/ray-kit/src/ray_kit/dashboard.py:516`, `packages/ray-kit/src/ray_kit/dashboard.py:169` *(+2 more)* |
| `PS-19` | E7 | med | readability | M | `cluster_status` is a 78-line function with a nested try inside a try inside a loop, doing five things | `packages/ray-kit/src/ray_kit/dashboard.py:275`, `packages/ray-kit/src/ray_kit/dashboard.py:294` *(+2 more)* |
| `PS-23` | E10 | med | observability | M | Dropped lineage events have no signal but a log line — and the authoring step sits inside the transport try/except | `packages/lineage-kit/src/lineage_kit/emitter.py:59`, `packages/lineage-kit/src/lineage_kit/emitter.py:61` *(+2 more)* |
| `PS-24` | E6 | med | duplication | M | The parent-resolution + namespace-defaulting ladder is copy-pasted in three modules | `packages/lineage-kit/src/lineage_kit/stage.py:46`, `packages/lineage-kit/src/lineage_kit/actor.py:50` *(+1 more)* |
| `PS-07` | E5 | low | config | M | storage resolves env by hand (`os.getenv` tuples) instead of pydantic-settings — and lineage-kit's docstring cites storage as an example of the pattern it… | `packages/storage/src/storage/client.py:23`, `packages/storage/src/storage/client.py:28` *(+4 more)* |
| `PS-08` | E12 | low | dead-code | S | `python-dotenv` is a declared dependency of `packages/storage` and is never imported | `packages/storage/pyproject.toml:10` |
| `PS-11` | E8 | low | resources | S | tracker owns an Engine + a long-lived Session but is not a context manager | `packages/tracker/src/tracker/_base.py:71`, `packages/tracker/src/tracker/_base.py:160` *(+1 more)* |
| `PS-12` | E11 | low | typing | S | `TrackerProtocol` omits `flush()`, which is in every backend's documented usage | `packages/tracker/src/tracker/protocol.py:11`, `packages/tracker/src/tracker/_base.py:137` *(+1 more)* |
| `PS-14` | E11 | low | typing | S | `@dataclass` used for a value object (Pydantic-only estate) | `packages/validate/src/validate/rules.py:12` |
| `PS-20` | E6 | low | duplication | S | The error-payload string is copy-pasted eight times across `dashboard.py` | `packages/ray-kit/src/ray_kit/dashboard.py:170`, `packages/ray-kit/src/ray_kit/dashboard.py:182` *(+6 more)* |
| `PS-21` | E1 | low | security | S | `proxy()` returns the raw, untruncated exception string — including the internal dashboard URL — to the browser | `packages/ray-kit/src/ray_kit/dashboard.py:578` |
| `PS-22` | E12 | low | dead-code | S | Orphaned `#:` doc-comment in `submit.py` documents a constant that no longer exists | `packages/ray-kit/src/ray_kit/submit.py:45` |
| `PS-25` | E5 | low | config | S | `LineageSettings()` is re-instantiated on every run-open in three lineage-kit paths — no cached accessor | `packages/lineage-kit/src/lineage_kit/stage.py:56`, `packages/lineage-kit/src/lineage_kit/actor.py:60` *(+3 more)* |
| `PS-26` | E6 | low | structure | S | `consume.py` imports the private `_Model` across a module boundary | `packages/lineage-kit/src/lineage_kit/consume.py:37`, `packages/lineage-kit/src/lineage_kit/schemas.py:69` |
| `PS-27` | E11 | low | typing | S | None of the five packages ships a `py.typed` marker | `packages/storage/pyproject.toml:22`, `packages/tracker/pyproject.toml:16` *(+3 more)* |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- packages/ray-kit/src/ray_kit/dashboard.py:160 — build_client swallows an unreachable OR token-rejected dashboard (AuthenticationError is in RAY_TRANSIENT_ERRORS) with `log.info(f"Ray dashboard unreachable at {dashboard_url}: {exc}")` and returns None: the group's only eager f-string log call, and an auth misconfiguration logged at INFO — the same debuggability defect PS-17 raises for logs().
- packages/storage/src/storage/client.py:59 — s3_client is annotated `-> Any # noqa: ANN401` even though the module defines `type S3Client = Any` at line 18 expressly as "a public alias for the boto3 S3 client"; the alias has zero users inside storage (s3.py imports mypy_boto3_s3's S3Client instead under TYPE_CHECKING), so the noqa is unnecessary and the boundary type it created is dead.
- packages/storage/src/storage/iiif.py:219 — the write-through `put_object` failure is caught by a bare `except Exception` and logged at WARNING, so a missing/misconfigured cache bucket degrades every read into a full IIIF re-fetch permanently and silently; combined with the NoSuchBucket-as-cache-miss branch at :202 this turns an unprovisioned bucket into invisible, unbounded upstream load.

### `ratch`

*Layout as it actually is:* `packages/ratch` is a src-layout library (`src/ratch`, hatchling, 55 .py / 7,582 LOC) with ONE entrypoint — the sanctioned Typer console script `ratch = ratch.__main__:main`. It is not a FastAPI service, so the fleet/fastapi-template layout rules don't apply; instead it is layered as: `core/` (media-agnostic kernel — `registry.py` declares a frozen Pydantic `Stage`/`ActorConfig`/`MediaGate`; `driver.py` holds three Ray Data drivers keyed off `StageShape`; `engine.py` the type-agnostic `add_columns`/`merge_insert` column engine + a JSONL `_ValueCheckpoint`; `dataset.py` the single `lance.write_dataset` seam; `jobs.py`/`runners.py` the Ray-Job + sealed-runner bindings), `features/` (the composition root: `stages.py` = corpus stage data, `columns.py` = the `FEATURES` dict of `_run_*` dispatchers, `ray_bindings.py` = stage-name→client factory, plus `projection.py`/`topic_tree.py`/`indexing.py`), `clients/` (four vLLM HTTP clients over a shared sync `VLLMTransport` = `httpx.Client` + `ThreadPoolExecutor` fan-out, each fronted by a `Protocol`), `ingest/`, `retrieval/`, `modalities/av/` (ffmpeg subprocess helpers), `model/` (Pydantic v2 transcriber model + PyArrow schemas), and `cli/` (a shared `_app.py` Typer app + 7 command modules registered by import in `cli/__init__.py`, root options carried on `typer.Context.obj` as a Pydantic `CliContext`). Error wiring is a one-class…

*Tests:* Zero test coverage. `packages/ratch/tests/` does not exist, `packages/ratch` is absent from the root `pyproject.toml` `testpaths` list (lines 188-189, which names eleven other package/service suites), and no file under `tests/` imports `ratch` — so even a test written today would not be collected by `make test`. Four docstrings assert coverage that does not exist: `core/__init__.py:5` ("see tests/test_core_contract.py and the P1.1 grep gate" — no such file anywhere in the repo), `features/columns.py:5` and `features/embed_columns.py:1` ("the seam tests drive with an offline fake"), and `clients/embedding.py:42` ("tests inject a deterministic offline fake"). The…

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `ratch-001` | E9 | **HIGH** | testing | L | The whole package has zero tests and is not even wired into pytest's testpaths | `packages/ratch/pyproject.toml:1`, `pyproject.toml:188` *(+4 more)* |
| `ratch-002` | E3 | **HIGH** | resilience | M | `ratch feature topics` reads the columns a fire-and-forget Ray Job has not written yet | `packages/ratch/src/ratch/core/jobs.py:133`, `packages/ratch/src/ratch/features/columns.py:327` *(+1 more)* |
| `ratch-003` | E1 | **HIGH** | security | M | Every `AWS_*` env var — including the secret access key — is copied into the Ray Job's runtime_env | `packages/ratch/src/ratch/core/jobs.py:47`, `packages/ratch/src/ratch/core/jobs.py:160` *(+1 more)* |
| `ratch-004` | E6 | **HIGH** | coupling | L | The library reaches into `runners/*` and three `services/*` modules that are not — and by ruling cannot be — its dependencies | `packages/ratch/src/ratch/cli/transcribe.py:61`, `packages/ratch/src/ratch/cli/transcribe.py:127` *(+8 more)* |
| `ratch-005` | E8 | med | resources | S | Every vLLM client opens an httpx connection pool that nothing ever closes | `packages/ratch/src/ratch/clients/base.py:34`, `packages/ratch/src/ratch/features/columns.py:134` *(+7 more)* |
| `ratch-006` | E3 | med | resilience | M | No retry or backoff anywhere on the model-server HTTP boundary, in a pipeline designed for hours-long runs | `packages/ratch/src/ratch/clients/base.py:43`, `packages/ratch/src/ratch/clients/base.py:49` *(+5 more)* |
| `ratch-007` | E5 | med | config | M | Three env-var namespaces, ten import-time `os.getenv` reads (plus two in-function), and no cached settings accessor | `packages/ratch/src/ratch/clients/embedding.py:26`, `packages/ratch/src/ratch/clients/caption.py:29` *(+8 more)* |
| `ratch-008` | E12 | med | dead-code | S | Two whole modules and four helpers/settings with no caller anywhere in the repo | `packages/ratch/src/ratch/lineage.py:1`, `packages/ratch/src/ratch/ingest/sources.py:1` *(+4 more)* |
| `ratch-009` | E6 | med | duplication | S | `ratch/ingest/sources.py` is an untested fork of the maintained `service_kit.lakehouse.sources` | `packages/ratch/src/ratch/ingest/sources.py:29`, `packages/ratch/src/ratch/ingest/sources.py:38` *(+5 more)* |
| `ratch-010` | E4 | med | error-handling | S | Index-build failures are swallowed at DEBUG and one helper is a literal `except Exception: pass` | `packages/ratch/src/ratch/ingest/ingest.py:433`, `packages/ratch/src/ratch/cli/media.py:182` *(+4 more)* |
| `ratch-011` | E6 | med | structure | S | `Stage.client` declares the capability the composition root should bind, and the composition root ignores it | `packages/ratch/src/ratch/core/registry.py:73`, `packages/ratch/src/ratch/features/ray_bindings.py:93` *(+3 more)* |
| `ratch-012` | E7 | med | readability | L | Six functions run 65-120 lines doing several distinct jobs, two of them near-duplicates of each other | `packages/ratch/src/ratch/cli/speaker.py:177`, `packages/ratch/src/ratch/cli/speaker.py:30` *(+4 more)* |
| `ratch-013` | E3 | med | resilience | S | Two ffmpeg subprocesses run with no timeout while every sibling ffmpeg call sets one | `packages/ratch/src/ratch/modalities/av/thumbnails.py:51`, `packages/ratch/src/ratch/modalities/av/thumbnails.py:70` *(+2 more)* |
| `ratch-014` | E5 | med | config | S | The library's FTS default is English while the CLI, the reindex path and the engine all default Swedish | `packages/ratch/src/ratch/ingest/ingest.py:357`, `packages/ratch/src/ratch/ingest/ingest.py:465` *(+4 more)* |
| `ratch-015` | E7 | low | readability | M | The Typer CLI prints through 54 bare `typer.echo` calls and hand-rolls column widths; `rich` is not a dependency | `packages/ratch/src/ratch/cli/pipeline.py:30`, `packages/ratch/src/ratch/cli/pipeline.py:37` *(+5 more)* |
| `ratch-016` | E7 | low | structure | S | `_run_in_process` passes arguments by mutating the global `sys.argv` | `packages/ratch/src/ratch/core/jobs.py:224` |
| `ratch-017` | E6 | low | coupling | S | Three modules import a leading-underscore private symbol across a module boundary | `packages/ratch/src/ratch/core/driver.py:34`, `packages/ratch/src/ratch/features/ray_bindings.py:21` *(+1 more)* |
| `ratch-018` | E11 | low | typing | S | Legacy `TypeVar` generics in the shared HTTP transport instead of PEP 695 | `packages/ratch/src/ratch/clients/base.py:13`, `packages/ratch/src/ratch/clients/base.py:24` *(+2 more)* |
| `ratch-019` | E3 | low | resilience | M | `_gate_filter` inlines every admitted doc id into one unbounded SQL `IN` list | `packages/ratch/src/ratch/core/driver.py:184`, `packages/ratch/src/ratch/core/driver.py:189` *(+1 more)* |

**Unfiled extras** — spotted by the verifier, not yet issues (file them with the epic named):

- packages/ratch/src/ratch/ingest/ingest.py:392 — f-string logging (`logger.info(f"loaded metadata for {len(...)} bildid(s)…")`), repeated at ingest.py:402, ingest.py:419, core/engine.py:312 and cli/media.py:183, against the lazy %-style used at every other logging call in the package
- packages/ratch/src/ratch/features/ray_av.py:62 — per-row `except Exception as exc: logger.warning(...)` inside the frame-extraction map: failures are counted nowhere and the stage returns a short table, so an APPEND_ROWS stage can silently produce fewer rows than inputs (same rule family as ratch-010, file not cited)

### cross-service — structure matrix and duplication

| ID | → | Sev | Cat | Eff | Finding | Sites |
| --- | --- | --- | --- | --- | --- | --- |
| `DUP-01` | E6 | **HIGH** | duplication | M | The governed-auth bootstrap (OIDC verifier + FGA provision/make_client) is copy-pasted into 8 lifespans | `services/catalog/src/catalog/main.py:89`, `services/lineage/src/lineage/main.py:86` *(+6 more)* |
| `DUP-02` | E6 | **HIGH** | duplication | M | lineage re-implements service_kit's service-door authenticator, and the two copies have already diverged | `packages/service-kit/src/service_kit/governed/dapr_auth.py:143`, `services/lineage/src/lineage/api/security.py:90` *(+1 more)* |
| `DUP-03` | E6 | **HIGH** | duplication | M | `authorize_produce` and `authorize_ingest` are two ~120-line copies of one dual-auth door | `services/medallion/src/medallion/api/produce_auth.py:41`, `services/medallion/src/medallion/api/produce_auth.py:58` *(+2 more)* |
| `DUP-04` | E6 | **HIGH** | duplication | S | catalog's `_bucket_client` builds boto3 directly, bypassing packages/storage and losing s3v4, path-style and timeouts | `services/catalog/src/catalog/services/warehouses.py:41`, `services/catalog/src/catalog/core/vending.py:195` *(+2 more)* |
| `X2` | E9 | **HIGH** | testing | S | `services/catalog/tests` and `services/lineage/tests` are not in `testpaths` — 18 tests guarding a commit-replay duplication bug and a privilege-escalation… | `pyproject.toml:188`, `services/catalog/tests/test_commit_idempotency.py:1` *(+2 more)* |
| `X4` | E10 | **HIGH** | observability | S | `configure_app_logging`'s allow-list is stale: `maintenance` calls it but is not on the list, while dead names `compaction` and `common` are | `packages/service-kit/src/service_kit/obs.py:27`, `services/maintenance/src/maintenance/service.py:45` *(+3 more)* |
| `X6` | E1 | **HIGH** | security | M | `search` is the only explorer service with no authn/authz code path at all — the chart's estate-wide OIDC/FGA env has nothing to bind to | `services/search/src/search/core/config.py:15`, `services/search/src/search/main.py:56` *(+3 more)* |
| `DUP-05` | E6 | med | duplication | S | `control_emit.py` exists twice, near-verbatim (136 and 141 lines), and only one copy is tested | `services/catalog/src/catalog/core/control_emit.py:41`, `services/maintenance/src/maintenance/core/control_emit.py:49` |
| `DUP-06` | E6 | med | duplication | S | Four byte-identical `health.py` liveness routers | `services/compute/src/compute/health.py:16`, `services/controlplane/src/controlplane/health.py:16` *(+2 more)* |
| `DUP-07` | E6 | med | duplication | S | The Arrow-IPC-stream encoder is hand-written at 7 sites, and the media-type string at 6 | `packages/service-kit/src/service_kit/lancekit/writer.py:151`, `services/annotator/src/annotator/annotations/wire.py:30` *(+8 more)* |
| `DUP-08` | E5 | med | config | M | The OIDC/FGA settings block is re-declared in 4 services despite `GovernedAuthSettings` existing for it | `services/catalog/src/catalog/core/config.py:175`, `services/lineage/src/lineage/core/config.py:37` *(+3 more)* |
| `DUP-09` | E6 | med | duplication | M | `apply_dapr_secrets` is written four times, once inline in a lifespan | `services/lineage/src/lineage/core/config.py:204`, `services/maintenance/src/maintenance/core/config.py:219` *(+2 more)* |
| `DUP-10` | E6 | med | duplication | L | Two OpenLineage kernels and four RunEvent builders, one of which documents itself as a duplicate | `packages/service-kit/src/service_kit/openlineage.py:30`, `packages/lineage-kit/src/lineage_kit/schemas.py:38` *(+4 more)* |
| `DUP-11` | E5 | med | config | S | The catalog identifier delimiter `$` is declared nine times through three different mechanisms | `services/catalog/src/catalog/core/config.py:29`, `packages/service-kit/src/service_kit/media/config.py:75` *(+7 more)* |
| `DUP-12` | E6 | med | duplication | L | The lance-service entrypoint is hand-assembled eight times; there is no `make_lance_service_app` | `services/catalog/src/catalog/main.py:178`, `services/lineage/src/lineage/main.py:122` *(+6 more)* |
| `DUP-13` | E6 | med | coupling | S | annotator re-implements `service_kit.governed.deps.make_auth_deps`, the module extracted from it | `services/annotator/src/annotator/api/security.py:43`, `services/annotator/src/annotator/api/security.py:109` *(+2 more)* |
| `DUP-14` | E3 | med | resilience | S | The same hand-rolled HTTP backoff loop exists in packages/storage and services/ingest, while tenacity is the estate's retry layer | `packages/storage/src/storage/iiif.py:89`, `services/ingest/src/ingest/fetch.py:82` *(+2 more)* |
| `DUP-15` | E3 | med | duplication | S | The Dapr-workflow scheduler is written twice, and the two copies' timeouts have already drifted | `services/ingest/src/ingest/__init__.py:214`, `services/flows/src/flows/lifespan.py:89` *(+2 more)* |
| `DUP-16` | E6 | med | duplication | M | viewer, search and annotator share a copy-pasted media-service lifespan | `services/viewer/src/viewer/main.py:34`, `services/search/src/search/main.py:32` *(+1 more)* |
| `DUP-17` | E5 | med | config | M | One Dapr secret store is named by seven different env vars sharing one default | `services/catalog/src/catalog/core/config.py:169`, `services/lineage/src/lineage/core/config.py:144` *(+5 more)* |
| `X1` | E7 | med | structure | M | DECISION ITEM: the estate runs three entrypoint families, two error taxonomies, three health conventions and two OTel wiring paths — the split is wider than… | `packages/service-kit/src/service_kit/__init__.py:90`, `services/gateway/src/gateway/__init__.py:266` *(+8 more)* |
| `X10` | E5 | med | config | L | Seven env-var prefixes across twelve services, with `LANCE_*` leaking into three services that otherwise use their own | `packages/service-kit/src/service_kit/config.py:26`, `services/catalog/src/catalog/core/config.py:27` *(+7 more)* |
| `X12` | E7 | med | testing | S | viewer/search test-seam factories build a different app than production — no problem handlers, no probes — so the exact regression their prod comments… | `services/viewer/src/viewer/main.py:115`, `services/search/src/search/main.py:75` *(+2 more)* |
| `X3` | E5 | med | config | S | `known-first-party` names 8 of the 19 real first-party import names — 11 packages sort into the third-party block | `pyproject.toml:153` |
| `X5` | E10 | med | observability | M | The fleet's own logging setup targets logger names that no longer exist, and `setup_otel` exports no logs at all despite claiming to | `packages/service-kit/src/service_kit/__init__.py:31`, `packages/service-kit/src/service_kit/__init__.py:34` *(+3 more)* |
| `X7` | E5 | med | config | M | gateway and ingest have no Settings class — 46 of the estate's 67 raw env reads live in those two services, several captured at import time | `services/gateway/src/gateway/__init__.py:109`, `services/gateway/src/gateway/__init__.py:114` *(+9 more)* |
| `X8` | E7 | med | fastapi | M | The four `make_service_app` services expose liveness only, and the chart points BOTH k8s probes at it — no readiness, no drain signal, though… | `services/compute/src/compute/health.py:16`, `services/controlplane/src/controlplane/health.py:16` *(+6 more)* |
| `X9` | E1 | med | security | S | `make_service_app` publishes /docs, /redoc and /openapi.json unconditionally, while all seven lance services gate them behind a *_DOCS setting | `packages/service-kit/src/service_kit/__init__.py:126`, `services/catalog/src/catalog/main.py:183` *(+3 more)* |
| `DUP-18` | E6 | low | duplication | S | medallion repeats the publish-trigger try/except at five sites | `services/medallion/src/medallion/services/ingest_trigger.py:117`, `services/medallion/src/medallion/services/media_produce.py:145` *(+3 more)* |
| `DUP-19` | E6 | low | duplication | S | Three hand-rolled `storage_options` builders bypass the shared `lance_storage_options` | `packages/service-kit/src/service_kit/lakehouse/objectfs.py:21`, `services/catalog/src/catalog/core/config.py:339` *(+1 more)* |
| `DUP-20` | E6 | low | coupling | S | service-kit exports two different `register_middleware` functions under one name | `packages/service-kit/src/service_kit/middleware.py:60`, `packages/service-kit/src/service_kit/media/middleware.py:22` |
| `DUP-21` | E6 | low | resilience | M | Seven outbound HTTP call sites build a fresh httpx client per call | `services/medallion/src/medallion/services/ray_submit.py:107`, `services/medallion/src/medallion/services/ray_submit.py:186` *(+5 more)* |
| `X11` | E4 | low | error-handling | S | ingest installs both handler sets, so its 422 body silently differs from its three fleet siblings | `services/ingest/src/ingest/__init__.py:47`, `services/ingest/src/ingest/__init__.py:64` *(+2 more)* |
| `X13` | E7 | low | resources | S | The three media apps' lifespans have no try/finally around `yield`, unlike the five other lance apps | `services/viewer/src/viewer/main.py:78`, `services/search/src/search/main.py:45` *(+3 more)* |

---

## Appendix B — the estate's actual shape (the structure mapper's matrix)

Read this before arguing about layout: it is what the twelve services *are*, not what they should be.

MATRIX (entrypoint | layout | config | error wiring | lifespan | DI | health | otel | dapr). gateway: bespoke module-level FastAPI in `gateway/__init__.py` | single-file proxy | NO Settings class — 15 raw os.environ reads + load_dotenv() inside `_routes()` | none (raises HTTPException 404/502 by design) | @asynccontextmanager, one AsyncClient(timeout 30/read 300) on app.state, closed in finally, no startup_complete flags | `request.app.state.*` read directly in the proxy handler (no dep wrappers) | `/healthz` only (chart uses it for BOTH probes) | in-code `setup_otel(app, "gateway")` | none (talks to sidecar by URL). compute: `make_service_app` | flat-module | shared `service_kit.config.Settings` (RASK_*, no lru_cache — built once in the factory) | `register_handlers` (DomainError/about:blank) | injected `make_lifespan`, httpx + ray client on app.state, no startup flags | Annotated aliases (`HttpDep`, `RayClientDep`) | `{api_prefix}/health` liveness only | setup_otel via factory | cron input binding (`pruner.py`). controlplane: `make_service_app` | flat-module | shared Settings + a bare `os.environ.get("RASK_PROJECT_URL_SCHEME")` inside the route | register_handlers | default_lifespan (settings only) | Annotated `ReaderDep` over an `@lru_cache` factory | `{api_prefix}/health` | setup_otel | none. flows: `make_service_app` | flat-module | shared Settings + own `FlowsSettings` (RASK_FLOWS_*, plain function, no lru_cache) | register_handlers | injected lifespan; httpx, run dict, optional Dapr WorkflowRuntime gated on DAPR_GRPC_PORT | Annotated aliases | `{api_prefix}/health` | setup_otel | Dapr Workflow (optional). ingest: `make_service_app` + `create_app()` wrapper | flat-module | NO service Settings class — 31 raw os.getenv reads across 12 modules (several at module scope) + one `IngestAuthSettings(GovernedAuthSettings, BaseSettings)` for auth only | register_handlers AND `install_problem_handlers` (both installed → RequestValidationError registered twice) | injected lifespan starts a Dapr WorkflowRuntime, non-fatal on failure; FGA resolved async | Annotated aliases + app.state seams | `{api_prefix}/health` + `/queue-health` | setup_otel | Dapr Workflow + NATS JetStream queue. catalog: own `FastAPI()` in `main.py` | api/v1/endpoints + core/ + services/ | `core/config.Settings` (LANCE_* aliases, `@lru_cache get_settings`, model_validators fail-closed) | `install_problem_handlers` (lance_namespace → RFC 9457) | long asynccontextmanager, startup_complete/shutting_down, Dapr secret store fail-closed, FGA/OIDC/vendor/emitters, isolated closes | Annotated aliases + router-level `dependencies=[Depends(authorize)]` | `/livez` + `/readyz` (make_probes_router with a namespace ready-check) | none in code — chart runs `opentelemetry-instrument`; `configure_app_logging()` at import | pub/sub (control events) + state store. lineage: own FastAPI in `main.py` | api/v1 template | `LineageSettings` (LINEAGE_*, lru_cache) + a stray `LANCE_AUDIT_ENABLED` | install_problem_handlers | asynccontextmanager, AGE psycopg pool + repository, FGA/OIDC, flags set | Annotated `SettingsDep`/`RepositoryDep` | `/livez` + `/readyz` with a real Cypher check | opentelemetry-instrument + configure_app_logging | pub/sub subscription + cron binding. medallion: TWO module-level apps (`producer.py`, `mover.py`) | flat `api/` + `services/` + `schemas/` (no v1 layer) | `MedallionSettings` (MEDALLION_*, lru_cache) + `LANCE_AUDIT_ENABLED` | install_problem_handlers | asynccontextmanager, DaprClient always built, FGA/OIDC, flags set | Annotated aliases | `service_kit.probes.router` (/livez + /readyz) | opentelemetry-instrument + configure_app_logging | pub/sub subscribe + publish. maintenance: own FastAPI in `service.py` | flat `api/` + `core/` + `services/` (no v1) | `MaintenanceSettings` (MAINTENANCE_*, lru_cache) | install_problem_handlers | asynccontextmanager, Dapr secret fetch, emitters, FGA + boto3-via-`storage.s3_client`, flags set | Annotated aliases | probes router | opentelemetry-instrument + configure_app_logging (but see F4) | two cron input bindings. viewer: own FastAPI in `main.py` | api/v1 template | `ViewerSettings(Settings, GovernedAuthSettings)` over `service_kit.media.config` (MEDIA_* + LANCE_*), lru_cache | register_handlers + install_problem_handlers | asynccontextmanager with NO try/finally around yield; sync `httpx.Client()`; OIDC/FGA best-effort | shared `service_kit.media.deps` Annotated aliases | probes router | opentelemetry-instrument + configure_app_logging | none. search: identical shape to viewer BUT `SearchSettings(Settings)` — no GovernedAuthSettings, no `api/security.py`, no auth dependency anywhere. annotator: same as viewer plus `DaprActor` mounted at import and three actor types registered in lifespan; extra top-level `annotations/` and `projects/` packages beside `api/v1/endpoints/`. REGISTRATIONS: chart + dev-micro + dockerfiles cover all 12 (catalog image carries catalog/lineage/medallion/maintenance/viewer/search/annotator; gateway/compute/controlplane/flows/ingest have their own). `[tool.ruff.lint.isort] known-first-party` lists 8 of the 19 real first-party import names. `testpaths` enrols 5 of the 7 service test dirs. NOTHING IN SCOPE WAS SKIPPED at the mapping level; per my role I did not deep-read individual endpoint bodies (only entrypoints, config, error wiring, health, DI/security and router aggregation modules).

And the duplication hunter's map of the shared plane:

Audited as a cross-cutting sweep over all 12 `services/*` and all 7 `packages/*` (grep-driven, then full reads of every hit). The estate has ONE shared platform library, `packages/service-kit`, split into four layers: base (`config.py` Settings, `exceptions.py` DomainError+handlers, `middleware.py`, `otel.py`, `obs.py`, `probes.py`, `dapr_publish.py`, `control_events.py`, `openlineage.py`, `slash.py`, `dependencies.py`), `governed/` (fga, oidc, deps, settings mixin, secrets, dapr_auth, audit, user_state), `lakehouse/` (ns_errors, objectfs, outbox, registries, trash, protection), `lancekit/` and `media/`; plus `packages/lineage-kit` (a SECOND OpenLineage kernel), `packages/storage` (the canonical S3 wrapper), `packages/ray-kit`, `tracker`, `validate`, `ratch`. The two sanctioned entrypoint layouts are real: `make_service_app` composes compute/controlplane/flows/ingest, while the 7 lance services (catalog, lineage, medallion×2, maintenance, viewer, search, annotator) each hand-assemble a module-level `FastAPI(...)` in `main.py`/`service.py`. The duplication concentrates almost entirely in that second layout: there is no `make_lance_service_app`, so eight entrypoints each re-write the same six-step boot (configure_app_logging → docs-gated FastAPI → install_problem_handlers → probes router → startup_complete/shutting_down flags → OIDC-verifier + FGA-provision block), and each of the four lance services re-declares the same OIDC/FGA/S3/Dapr-secret settings blocks under its own env prefix rather than mixing in the `GovernedAuthSettings` that already exists for exactly that. Nothing in scope was skipped; `runners/htr` is out of scope (sealed, not a workspace member) and was not read.

---

## Appendix C — turning this into issues

Each row of Appendix A is one issue. The body writes itself from the finding's own fields:

```
Title:  [<epic>] <finding title>
Labels: python-audit, <epic-id>, sev:<high|medium|low>, area:<scope>, cat:<category>
Body:
  ## Sites
  <file:line list from Appendix A / the JSON>
  ## Why
  <the finding's `why` — it states the failure scenario, not the rule>
  ## Fix
  <the finding's `recommendation` — every one names a concrete change>
  ## Done when
  <the recommendation's test clause; most name the test to add>
  ## Provenance
  open_python-audit.md · <id> · verified <CONFIRMED|ADJUSTED>
```

The machine-readable source sits beside this file as **`open_python-audit.findings.json`** — all 304
findings keyed by the same ids. High and medium findings carry their full `why` + `recommendation`;
low findings are index-only (title + sites), because the tables above already carry their one-liner.
The verifier's `evidence` and `verify_note` prose is **not** in the JSON — at full fidelity the file
was 997 KB, past this repo's own 500 KB `check-added-large-files` policy, so it was trimmed rather
than force-pushed past the gate. That prose is quoted for every high finding in the epic sections
above, and the audit workflow regenerates the full form on demand. Delete both files with this plan
when the backlog is drained. A filing loop is mechanical:

```bash
python3 - <<'PY'
import json, subprocess
d = json.load(open('open_python-audit.findings.json'))
# Files every HIGH. The JSON carries no epic field — pick the rest by id from the epic tables.
for g in d['groups']:
    for f in g['findings']:
        if f['severity'] != 'high':
            continue
        body = f"## Sites\n{chr(10).join('- ' + s for s in f['sites'])}\n\n"
        body += f"## Why\n{f['why']}\n\n## Fix\n{f['recommendation']}\n\n"
        body += f"## Provenance\nopen_python-audit.md - {f['id']} - {f.get('verdict')}\n"
        subprocess.run(['gh', 'issue', 'create', '--title', f"[{f['id']}] {f['title']}",
                        '--body', body, '--label', 'python-audit'], check=True)
PY
```

**Do not batch-file all 304.** Wave 1 (E1 + E2 + E9) is 43 issues and is the only set worth filing
before the first fixes land — the rest will be re-scoped by the refactors in waves 2-3.
