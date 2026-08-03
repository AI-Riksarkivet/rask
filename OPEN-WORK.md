<!--
  THE one register. Moved out of docs/ on 2026-07-29: this is a working file — a list of what is
  left to do — not published documentation, and it had no business sitting in the docs site beside
  architecture reference.

  Seven trackers were folded in below, whole rather than summarised. They were separate files each
  carrying its own open list, so "what is left" had to be reassembled from seven places every time
  anyone asked. Their full text is here; nothing was reduced to bullet points, and git history holds
  the originals at their old paths.

  NOT folded in, deliberately: DECISIONS.md (an append-only log of WHY, still cited from ~50 places
  — folding it into a TODO list would lose the reasoning people look up) and docs/audits/ (the
  long-form evidence behind verified claims, with the adversarial second pass that makes them worth
  anything).
-->

# Open work — the backlog that must survive the merge

This file exists because the open items were only ever recorded as **session task IDs** (`#103`, `#124`, …)
in a task tracker that does not outlive the session, and in a re-pin diff that was applied and deleted by
design. After the merge nobody in rask knows what "#103" means.

So every entry below is **self-describing**: what it is, why it is open, where the code lives, and what
would close it. The old task numbers are kept only as a cross-reference for anyone reading the lance-ns
history. **`docs/architecture/lance-ns-merge.md` P0 copies this file into rask** — it is not left behind,
and P8 reconciles it rather than dropping it.

Status as of 2026-07-27. The twenty UX-goal conditions are met — the goal tracker is retired (git
history); **the durable artifact is [`GOAL-UX-REACTIVE-EVIDENCE.md`](GOAL-UX-REACTIVE-EVIDENCE.md)**.
Everything here is what remains *after* that.

---

## A. The merge forces this one

### A1 · The media corpus must leave its node hostPath *(was #103)*

**What.** `services/{viewer,search,annotator}` read the corpus from a node-local `hostPath`
(`/var/media-corpus`, `chart/templates/media.yaml:126`), staged from the lance-audio box. `MEDIA_DB_ROOT`,
`MEDIA_DB` and `MEDIA_DESCRIPTOR_DIR` all hang off `media.corpusMountPath`; 10+ files across the three
services read it.

**Why it is open.** It was correct for a single-node kind cluster and deliberately deferred — "NO data move:
the corpus stays node-local", per the template's own comment.

**Why the merge forces it.** A hostPath binds a pod to whichever node holds the data. The merge plan's P4
already rules **"no hostPath ships"**.

**What closes it.** Two halves, and they should be decided separately:
- *Portable:* register the corpus as **catalog-governed project tables** (the intended read-plane shape).
  This survives any destination and is the part worth doing first.
- *Destination-specific:* a PVC, or a rustfs-backed corpus bucket on rask's operator Tenant. Decide this in
  P4 against the cluster it will actually live on — deciding it in lance-ns means deciding it twice.

---

## B. Built halfway — the second half is named and small

### B1 · No actor type and no workflow are registered *(was #124, second half)*

**What is done.** `lance-statestore` is live: `state.postgresql` on the AGE Postgres, DSN resolved from
OpenBao through `lance-secrets`, `actorStateStore: "true"`, scoped to `catalog` + `annotator`. Per-subject
user state round-trips through it and is proven across browser contexts.

**What is not.** The flag that gates actors *and* workflow is on and **nothing uses it**. No actor type is
registered; no workflow is registered.

**What closes it.** An actor type hosted by a service in the component's `scopes`, proven by a round trip
through the sidecar. Keep `tests/unit/test_invariants.py`'s scope check — an app missing from `scopes` gets
"component not found" and every user's saved work 503s, logged by the sidecar and noticed by nothing else.

### B2 · The notification inbox has no actor *(was #128)*

**What.** Read/dismissed state for notifications is per-tab. The bell itself is done and estate-wide (all
four zones, shared `@repo/api/runs-feed`), because `GET /runs` already carries the lifecycle — but *read*
and *dismissed* are per-subject state the run feed cannot carry.

**Why it is open.** It needs B1.

**What closes it.** One actor per subject inbox, unread counts that cannot race, expiry via reminders rather
than a sweeper cron.

### B2b · ratch's runner imports become the Ray-native name seam *(new, 2026-07-27)*

**What.** `packages/ratch/cli/{speaker,transcribe}.py` still lazily import `from runners.diarize.diarize
import …` — repo-relative module paths from the lance-audio heritage, working only when the repo root is on
`sys.path`. The runners tree deliberately carries no `__init__.py` glue any more (`a4cf8f6`) and runners are
sealed non-members of the workspace, so these imports are dead code walking.

**What closes it.** When ratch is wired (the pipeline step): ratch passes runner NAMES and each runner's
`pyproject.toml` as the Ray worker `runtime_env`; the actor module imports on the WORKER. The contract is
stated in `runners/README.md`. Do not resolve this by making `runners.` importable again.

### ~~B3 · Annotation projects are designed, not built~~ **CLOSED 2026-07-31, with evidence** *(was #122)*

**What.** [Design — annotation projects](#design--annotation-projects) (below) — entities, both state machines, the authz doors, what a
publish emits, and a slice plan.

**Closed by the open_anno build (2026-07-31).** Slices `S1`–`S9` are live end to end, driven in chromium
against the k3s cluster with real Dapr actors, a real OpenFGA store and a real catalog: project created →
items sent → claim (lease from the PROJECT's `lease_seconds`, captured at send) → submit → the review
row's three distinct actions with the server's self-review 403 rendered verbatim → freeze → publish →
**a real governed Lance table** (`silver$vasa-publish-…`, tag `publish-<token>`, catalog `describe`
resolves `s3://lance-catalog/…`). Screenshots examined per surface; two defects found by LOOKING were
fixed (actions-column clipping; `Reopen` rendered on a published project — the details listing now
empties `legal_events` once the project is in a frozen state, pinned by
`tests/unit/test_project_event_endpoints.py::test_details_on_a_frozen_project_carry_no_task_events`).

What the build added beyond the S-slices as specified, each red-first:

- **The publish transport existed only as a Protocol** — nothing called `run_publish`, so `publish`
  stranded projects in `publishing` forever. Now: `projects/lakehouse.py` (`CatalogPublisher` over the
  lance-ns SDK — `mode=exist_ok` create, tag create with catch-conflict-compare-version convergence,
  facet payloads stripped BARE for `X-Lance-Run-Facets` because the catalog stamps and 400s `_`-keys)
  plus the **publish watchdog reminder** on the project actor (`PUBLISH_REMINDER`, due ~1 s, period 60 s
  until terminal) — the crash-recovery caller the saga's "safe to call again" contract never had.
- **The target namespace is PINNED with the publish token** (`pending_target_namespace`) — the doors were
  checked against a namespace the actor never recorded, so a crash-recovered saga would have had to guess.
  A retry naming a different namespace is refused (a different namespace is a different table id).
- **The saga's table id used `.` where the catalog's delimiter is `$`** — the table would have landed at
  the catalog ROOT while FGA authorized `namespace:silver`. Fixed (`saga.py::CATALOG_DELIMITER`).
- **S4 landed**: catalog `create` accepts `source`/`source_version`/`X-Lance-Run-Facets` (reusing
  `_merge_source_pin`/`_parse_run_facets`, same forge-guard), so the FIRST write of a derived table
  finally carries a reproducibility pin + producer facet (`tests/unit/test_create_lineage_pin.py`).
- **The read surface A1 needed did not exist**: `GET /projects` (via a new `TenantProjectsActor` index,
  registered at create), `GET /projects/{id}` + `legal_events` derived from the machine tables, and
  `GET /projects/{id}/tasks?include=details` (bounded fan-out, per-task `legal_events`) — the UI renders
  the transitions the backend supplies and holds no copy of the machine.
- **Dapr's `ActorProxy` dispatches WIRE names** (`ListProjects`), not Python names (`list_projects`) —
  every cross-actor call in the plane raised `AttributeError` in-cluster while every mocked unit test
  stayed green. `projects/proxies.py::typed_proxy` translates from the interface's own `@actormethod`
  metadata; a sweep test forbids raw `ActorProxy.create` outside it
  (`tests/unit/test_actor_proxy_names.py`).
- Frontend: the zone's landing is the projects view (S9 — `DataSelection` moved to `/browse`), the queue
  is `@rask/ui/data-table` with the honest lease chip (wall-clock `expired` beats a stale "held"),
  publish confirm/progress/failed-retry panels, BFF proxies + a valibot-parsed client (writes carry
  explicit `content-type` — browser `fetch` defaults a string body to `text/plain` and FastAPI 422s),
  the zone's vacuous `test` gate made real (`"test": "vitest run"`), 7 new hermetic e2e specs, and
  `@rask/ui` gained a `textarea` component.

**Second wave, same day (the product pass — "so much missing" was right):** the funnel and the
labeling substance landed after the first close:

- **Send-to-project from media** — search results AND the atlas selection toolbar carry
  "Send to project…" (append to a draft/labeling project or create one around the selection),
  via a media→annotator BFF proxy; hermetic e2e (`media/e2e/send-to-project.spec.ts`, 3 specs:
  send with descriptor key-path + dataset provenance, create-and-send, the 403 surfaced).
- **The labeling task is specified at create** (`LabelSchema`: class names + shape kind in the
  create dialog; the taxonomy renders on the detail page) — the canvas constraining its tools to
  it is still open.
- **Canvas → task draft (S10 first half)**: a task-opened canvas (`?task=` on the queue's Annotate
  link) snapshots the saved unit's rows into the task DRAFT (revision-guarded), so drawn shapes
  travel into the publish; `draft-shapes.ts` pins the column-for-column mapping. The full S10
  cutover (deleting the media-plane write) remains, deliberately last.
- **Bulk review** (the LLM-as-judge foundation): queue row selection + "Accept N reviewed" —
  per-task server-gated, failures reported individually (the e2e for it caught a real
  derived-after-clear bug). The LLM-labeller import ride (`prediction_import` → drafts with
  `origin: model`) and the embedding view need the assist runner (mock-labelled today).
- **FGA on the annotator is chart-wired** (`media.yaml`: LANCE_FGA/OIDC under `auth.enabled`,
  annotator only; render-pinned by `tests/unit/test_annotator_auth_env.py` both ways).
- **The annotator sidebar is project-centric** — the Corpus group (Search/Atlas/Graph cross-zone
  links) removed; the flow runs media→send→project, not annotator→launcher.
- **@rask/atlas extraction (planned, not done):** the WebGPU atlas (`media/src/lib/atlas/` —
  AtlasMap, gpu-scatter, cross-filter, legend/geometry/colors) should hoist to
  `frontend/packages/atlas` exactly as the pixi engine did (zone-agnostic props, no `$app/*`,
  transport injected), so the annotator's bulk-labeling view can embed the same selection surface.
  Blocked on nothing technical; it is a day of careful extraction + zone rewiring.
- **The publish's reproducibility pin — SHIPPED in the third wave (below).** (The 2026-07-31
  discovery stands as history: the spec-generated `lance_namespace_urllib3_client.create_table`
  cannot send the S4 query params, which is why the transport is a direct HTTP call.)
- **UI-language rename SHIPPED (2026-07-31):** the annotator says "Labeling tasks" / "items"
  everywhere user-facing; "Projects" is platform-only. The wire rename remains (below).
- **Assignment UI SHIPPED (2026-07-31):** Assign… on assignable rows (legal_events-driven),
  named recipient, server pins the lease; e2e proves the pinned chip. Consensus remains (below).
- **Vocabulary: "project" is PLATFORM-level (owner ruling 2026-07-31).** The annotator's grouping
  collides with the estate tenant. Rename to the CVAT-shaped ladder: the annotator's unit of work
  is a **labeling TASK** (today's `AnnotationProject`) holding **items** (today's `Task`). Two
  layers: (1) UI language — cheap, do first; (2) the wire/state rename (`annotation_project` FGA
  type, `AnnotationProjectActor`/`AnnotationTaskActor` ids, `/projects`+`/tasks` endpoint paths,
  `can_create_annotation_project`) — actor ids and FGA types carry live state, so this is its own
  red-first slice with a state-migration note, not a drive-by.
- **Assignment UI ✓ (above) + consensus v1 SHIPPED in the third wave (below).** Task settings
  surface: description ✓, review-required ✓, lease ✓, label schema ✓, assignees ✓, consensus N ✓;
  still missing: an instructions page, and the manager MERGE step (deliberately not built — see below).
- **LS/CVAT parity gaps, named** (beyond the §2 design diff): export serializers (COCO/YOLO/CSV/HF
  — owner-deferred, rides the P7c exporter), honeypot/ground-truth items, annotator analytics
  (lead-time exists per item; no dashboard), webhooks (control events exist on pub/sub — a consumer
  away), ML-assist beyond the mocked runner. The published-table + lineage story EXCEEDS both
  references (neither has governed provenance).
- **Live witness of the canvas→draft leg is PENDING**: the first live drive published real tables
  (Dapr actors + catalog + FGA seed, all in-cluster); the second drive (canvas draw → draft →
  publish-with-shapes, script kept at `frontend/microfrontends/annotator/drive2.tmp.mjs`) was
  interrupted when tilt took the cluster back mid-drive with `auth.enabled` on and the in-flight
  conditional-grant FGA model failing to provision (annotator now fail-closes 503, correctly).
  Once the model fix lands: seed a corpus (the media pods mount an EMPTY, read-only emptyDir —
  `scripts/seed_demo_corpus.py` + a writable corpus mount or the A1 corpus move), sign in, run
  drive2. The hermetic halves are all green (8 annotator + 3 media specs).

**Third wave (2026-07-31): the pin + consensus v1, adversarially audited.**

- **The reproducibility pin is BUILT end to end.** `ItemSource.dataset_version` captured at send
  (`models.py`; the media dialog reads it off the descriptor's row table and the e2e asserts it in
  the POST body), `build_plan` collects per-dataset versions + an `sources_uncaptured` count,
  `source_pin` pins ONLY when every published item shares one dataset at one captured version
  (`publish.py`), the saga passes `source`/`source_version` to the publisher (`saga.py`), and
  `lakehouse.py::_HttpCreateApi` carries them as the S4 query params over direct httpx (same
  signature as the SDK seam; ≥400 → the SDK's `ApiException`, so `translate_catalog_errors` sees one
  error surface). The facet's `sourceDatasets` entries carry their captured `version`.
- **Consensus v1 is BUILT (replica items, no merge).** `consensus_n` (1–5) on the labeling task;
  send seeds N independent replica items per sent item with deterministic ids (`{gid}-r{k}`,
  `Task.replica_of`), capacity-capped at items×N ≤ 1000; the SAME annotator may hold at most ONE
  replica of a group — enforced server-side at claim AND assign (409 naming the held replica and the
  rule); the publish emits EVERY accepted replica's rows and the run facet reports agreement COUNTS
  (`consensus: {n, groups, perfect_agreement_groups}` from per-group label multisets) — **no merged
  truth is invented; the manager merge/adjudication step is deliberately not built.** UI: consensus
  field in the create dialog, `replica k/N` chip in the queue, the 409 surfaced verbatim (3 new
  hermetic e2e specs; screenshots examined — the chip moved under the key after the inline version
  clipped the actions column).
- **The adversarial audit (fresh-context) earned its keep — all findings fixed same-day:**
  **B-1 (critical):** `CreateProjectRequest` lacked `consensus_n`, so Pydantic silently dropped the
  field at the ONE entry point and every project persisted `consensus_n=1` — the whole replica plane
  was dead code on the real path while every mocked layer stayed green. Fixed + pinned by
  `test_consensus_n_travels_from_the_create_payload_to_the_persisted_doc` (asserts the PERSISTED
  doc, not the echo). **A-1 (medium):** a plan mixing captured items with items that recorded NO
  dataset still pinned — `sources_uncaptured` now blocks the pin (parametrized refusal case added).
  **A-2 (medium):** `_HttpCreateApi` was only tested through an injected fake — the transport itself
  (URL quoting of `$`, pin params, arrow content-type, error translation) now has direct tests.
  Plus: capacity-guard and assign-path-guard tests (B-2/B-3), a stale §7.2 comment (A-3).
  Known boundary, documented not fixed: a replica released WITHOUT submitting was neither held nor
  worked, so the same annotator may later take a different replica of that group — matches the
  "holds or worked" rule; tighten to "ever touched" only with a per-task touch log.
- **The live READ-edge witness is PENDING with the same blocker as drive2**: the in-cluster
  annotator fail-closes 503 ("Authorization is enabled but unavailable") on the in-flight
  conditional-grant FGA model, so the pin was verified hermetically at every seam (capture → plan →
  pin → saga → HTTP params → the catalog's own `test_create_lineage_pin.py`) but not yet observed as
  a lineage graph edge on the cluster. Run the drive once the model lands.
- **LS/CVAT task-management diff after this wave:** consensus/overlap now matches Label Studio's
  N-annotations-per-item shape (LS: `maximum_annotations` + agreement; CVAT OSS has no native
  consensus — honeypot/GT only). Still LS-ahead: adjudication/merge UI, per-annotator agreement
  dashboards, instructions page. Still CVAT-ahead: dense video/interpolation tooling. Ours alone:
  the two-door governed publish, the byte-identical replay, and the lineage pin — neither reference
  has governed provenance at all.

**Fourth wave (2026-08-03): adjudication v1 + instructions — the two LS-parity gaps closed, then a
17-agent adversarial workflow audit, all confirmed findings fixed same-day.**

- **Adjudication is a PICK, never a blend** (`Adjudication` in `models.py`; `adjudications:
  {group → {task_id, by, at}}` on the project). The manager names ONE accepted replica canonical
  (`PUT /projects/{id}/adjudications/{group}`, `can_manage` — it decides which OPINION wins, which
  is distribution authority, not review); every replica's rows still publish and the facet carries
  the pick WITH attribution (`consensus.adjudications[group] = {task_id, by, at}`). UI: an
  Adjudication card on the detail page (pick/re-pick on accepted members only, stale-pick warning,
  Withdraw) + "canonical" chips in the queue. Synthesizing a merged shape set remains deliberately
  unbuilt — it would put words in annotators' mouths; a consumer filters canonical rows by the
  picked ids.
- **Instructions** (`AnnotationProject.instructions`): the annotator-facing HOW, set in the create
  dialog, persisted at the entry point (pinned by a create-surface test — the consensus_n B-1
  lesson applied on day one), rendered on the detail page.
- **The audit's confirmed findings, all fixed:** (1) *membership was a string-prefix check at BOTH
  the actor and the publish* — client-suppliable ids like `g1-r1-r2` (a member of group `g1-r1`)
  passed every check on `g1` and the facet would canonicalize another group's work; now the actor
  requires the exact `{group}-r{digits}` shape and `build_plan` refuses any pick not a member of
  `plan.replica_groups[group]` (membership by `replica_of`, the authoritative grouping). (2) *no
  removal path* — the publish refuses a stale/groupless pick (correctly), so one wrong pick wedged
  publishing permanently; now `DELETE /adjudications/{group}` (+ Withdraw in the UI) clears it,
  idempotently, refused once provenance freezes. (3) *a pick silently survived reopen → resubmit →
  re-accept*, canonicalizing content the adjudicator never saw; now `task_state_changed` voids any
  pick whose target leaves `accepted`, and the publish-side refusal stays as the backstop for the
  report-failure window. (4) *the facet dropped by/at* — now carried. (5) the `Adjudicate` wire
  name is pinned in `test_actor_proxy_names.py`; the 409 e2e asserts the pick did NOT land; the
  panel has a no-Pick-on-non-accepted negative spec.
**Fifth wave (2026-08-03): the LIVE witness — driven in chromium against the k3s cluster with real
OIDC (dex session), real OpenFGA, real Dapr actors.**

- **The fleet-wide FGA 503 is FIXED at its root**: `fga.provision` passed only `schema_version` +
  `type_definitions` to `WriteAuthorizationModelRequest` and silently dropped `model["conditions"]`
  — the moment the time-boxed-grants model landed (`non_expired_grant`), OpenFGA 400'd every
  provision ("condition … is undefined for relation reader") and every FGA-enabled service
  fail-closed 503. One-line fix + `test_fga_provision.py` pins it. The annotator now provisions,
  checks, and answers real 403s/200s in-cluster.
- **Witnessed live, end to end** (today's UI on the dev zone with a real sealed session → BFF →
  in-cluster annotator): create with `consensus_n=2` + instructions (FGA-gated, persisted); ONE
  send seeding TWO replica items from the real actor; alice claiming r1 and being refused r2 with
  the one-replica-per-annotator 409 verbatim on screen; bob claiming+submitting r2 under HIS
  verified dex sub; the adjudication PUT landing (canonical chips + Withdraw); freeze → publish
  running the real saga (reading tasks → building plan → `creating table
  silver$consensus-live-41965_…`) and failing EXACTLY at the recorded service-identity residual,
  with the failure narrated and retry offered (A4 behaving as designed).
- **The live leg caught what hermetic e2e structurally cannot**: the annotator zone's projects BFF
  proxy exported only GET/POST — the adjudication PUT/DELETE 405'd against the real server while
  every `page.route`-mocked spec stayed green. Fixed (+ the media-zone verb set audited).
- **Found on the cluster, needing OWNER action:** (1) both kueue installs' webhooks are dangling
  fail-closed with every controller pod dead — since 07-31 every Deployment mutation (tilt's app
  updates included) fails on `mdeployment.kb.io`; unblock with
  `kubectl patch (mutating|validating)webhookconfiguration <kueue…> --type=json -p '[{"op":"replace","path":"/webhooks/N/failurePolicy","value":"Ignore"}]'`
  per hook, or revive/uninstall kueue. (2) `rask-web-home` crash-loops: tilt's in-container
  `bun run build` dies exit 137 while the chart's 6Gi dev tier is in place — the imaged build is
  fine; the zone images need a full rebuild once deploys are unblocked (the web pods have been
  serving the 07-31 UI since). (3) ~~The publish's catalog identity~~ **DECIDED and SHIPPED same
  day (sixth wave, below).**

**Sixth wave (2026-08-03): the publish identity — decided, built, and the FULL live witness landed.**

- **The decision:** dex has no client-credentials grant, so machine identity is a dedicated
  password-grant SERVICE ACCOUNT (`publisher@rask.internal`, chart-declared in dex's static users).
  `lakehouse.publish_token` mints a FRESH token per publish — nothing long-lived is stored anywhere,
  so nothing can go stale (the exact failure the hand-pinned July token produced live). Precedence:
  a set `MEDIA_CATALOG_TOKEN` still wins (prod may pin a token its own machinery rotates).
- **Secrets per the estate's rule:** the account's password never rides pod env — it is seeded into
  OpenBao (`secret/lance` gains `publisher-oidc-password`, chart seed job) and fetched through the
  Dapr secret store (`lance-secrets`, already scoped to the annotator) via `fetch_required_secrets`,
  fail-closed. Only non-secret coordinates (`MEDIA_PUBLISH_TOKEN_URL`/`CLIENT_ID`/`USERNAME`) ride
  env; the render test asserts the password appears in NO env block. A minting failure surfaces as
  `publish_failed` with the IdP's words — never a stranded `publishing`.
- **The live witness, complete:** `silver$consensus-live-41965_…` PUBLISHED by the minted service
  identity — lineage records the run COMPLETE with the publisher's verified dex sub as author. Then
  the PIN: a second project whose send captured `where` + `dataset_version=1` published
  `silver$pin-live-b_…`, and the lineage graph now shows the READ edge
  `silver$pin-live-b_… → silver$consensus-live-41965_…` with run inputs `{name, version: "1"}` —
  §7.2's reproducibility provenance, observed end to end on the real cluster.
- **Noted along the way:** dex restarts rotate its in-memory keys, killing every outstanding token
  (pods restarted after dex recover; a persistent-storage dex would remove the class); the annotator
  maps `UnauthenticatedError` from its own verify path to a 500 rather than 401 on one route
  (cosmetic, worth a handler row); the lineage read API does not surface custom run FACETS (the
  consensus/adjudications facet is delivered to the catalog — unit-pinned — but `/runs` returns no
  facet payloads; a lineage-service read gap, not a write one).
- **The modality / task-template matrix, audited honestly (owner asked, 2026-08-03):**
  **Image** — full loop, live-verified (the only modality ever driven end-to-end). **Audio/video** —
  the SURFACES exist (`viewer/registry.ts` maps `audio: AudioViewer` / `video: VideoViewer`;
  `@rask/engine` ships the temporal waveform lane, video reuses ImagePlugin; `Shape.t_start/t_end`
  carry spans; `MediaRef.kind` is image|audio|video) but NO audio/video item has ever been sent
  through the labeling-task loop — untested, and the send surfaces only produce image/text items
  today. **Text** — chunk-level tagging is the media plane's original job (tags on transcript
  chunks, `ShapeType` has `tag`/`text`, `Shape.text`); character-offset span labeling (NER-style)
  has NO tool. **Document tasks (DocQA, reading order)** — the DATA model can carry them
  (`Shape.group` for linking, free-form `attributes: dict[str,str]`, `LabelSchema.attributes`) but
  there is NO first-class relation/sequence editor and NO task-template system (nothing like Label
  Studio's labeling-config): a labeling task's "shape" is classes+geometry only. That template
  layer — declaring per-task input modality, tools, relations, output schema — is the real
  flexibility gap; the storage model underneath it is already generic.
- ~~**NOT tested: the label-assist runner.**~~ **DONE in the seventh wave (below).**

**Seventh wave (2026-08-03): AI-assist made real and pluggable, task templates, and the dev estate
made seedable — the `open_label.md` waves, folded here as that file retires.**

- **W1 — assist is real and PLUGGABLE, proven not claimed.** The `assist-runner` image was
  dagger-built for the first time (3.9 GB: torch + GroundingDINO-tiny + SAM), pushed, and deployed
  (`rask-assist`); driven live it returned a genuine detection (conf 0.7476) and a real SAM polygon
  (0.637) into the canvas Review queue as `prediction` rows. Then the seam that makes it a
  *platform*: a producer **registry** (`MEDIA_ASSIST_BACKENDS` name→URL, longest-prefix routing,
  `assist_url` fallback, honest mock when bare — `backend_for` + `tests/unit/test_assist_registry.py`),
  and **INSID3 wired as a genuine SECOND backend** (`runners/insid3/`, same `/v1/assist` contract,
  a different MODE: in-context reference propagation on one frozen DINOv3) driven end to end —
  the canvas bar offers **Detect · Segment · insid3** from `/api/config assistProducers`, and a
  drawn region returned a real 15-point polygon as `model:insid3`. Adding a model is now a config
  entry, not a code change. Also landed: the previously-missing hermetic assist→review spec.
- **INSID3 evaluated on REAL Riksarkivet IIIF pages** (A0060198, 18th-c. cursive, RTX PRO 6000):
  0.4–0.7 s/page at ViT-S@1024 — interactive. Region-level propagation is good (a masked column
  found the written leaf on the NEXT page); line-level is not (one line collapses to "the written
  area"). So: text-REGION / layout pre-labeling, not line segmentation. Apache-2.0; DINOv3 weights
  are Meta-gated, so `runners/insid3/convert_weights.py` rebuilds the original checkpoint from the
  HF safetensors (inverse key mapping, k-bias zero-filled, verified by a STRICT load). Blackwell
  needs the cu128 torch wheels — cu126 has no sm_120 kernels.
- **W2 — task templates v1 (the Label-Studio-config equivalent).** A declarative `TaskTemplate`
  (`kind`/`modality`/`tools`/`required_labels`/typed `attributes`/`enforce`) on the labeling task,
  **captured onto every item at send** (the `review_required` pattern), **enforced at SUBMIT inside
  the task actor** so it holds for any caller, and stamped into publish table-properties + the run
  facet. Create-dialog presets pick it; the detail page wears the chip. Proven LIVE against the
  real service: a violating submit is refused with the server's own words —
  `409 "submit (template classification allows tools ['tag'] — shape … is bbox)"` — and a
  conforming submit lands `accepted`.
- **A real bug the live drive exposed: every actor-side precondition was answering 500, not 409.**
  Dapr serialises an actor's exception into an HTTP 500 body, so the endpoints' `except
  IllegalTransition` — the code that turns a refusal into a 409 *with the reason* — stopped
  matching the moment a check moved into an actor. `projects/proxies.py::_translating` now unwraps
  it at the one seam every actor call already goes through, so existing handlers work as written
  and future actor-side preconditions inherit it.
- **`make seed-dev` — the estate is seedable in one command.** A fresh install serves an EMPTY
  corpus (`media.corpus.mode` defaults to `emptyDir`), which is why `/media` found nothing,
  `/annotator` had no page and `/lakehouse` listed no tables — "not seeded" was indistinguishable
  from "not built", and the two existing seed scripts were wired to no command. Now: a searchable
  multi-document corpus (10 pages / 3 documents + an FTS index), real IIIF pages into RustFS
  **registered** as `bronze$pages`, and a labeling task with items — idempotent, and
  **self-configuring** (it reads the dataset name from the live deployment's `MEDIA_DB` and the
  tenant from the catalog's `/v1/me`, so the fixture cannot drift from the config that reads it).
  Two traps worth keeping: a host-written corpus is mode 0600 and the services run as uid 10001,
  and **Lance reports that EACCES as "Object at location …/tokens.lance not found"** — a not-found
  for a file that is present, which reads exactly like a corrupt or version-skewed index; and a
  fixture named anything other than what `MEDIA_DB` asks for produces "dataset 'transcripts_v2' not
  found" with the corpus sitting right there.
- **Two dead P0 scaffolds removed:** `/lakehouse/catalog` and `/lakehouse/admin` rendered "The Data
  zone (P0 scaffold). Routes move here from apps/web in P3." — a 200 that looks intentional and
  teaches nothing. Both now redirect to their first real child, matching the `governance` group.
- **The wave's own adversarial audit (3 lenses, 25 findings) — what it caught and what it left.**
  Fixed in the same commit, each with the regression test that was missing: (1) **enforcement was
  vacuous on an empty shape set** — every rule is a per-shape test, so claim+submit with no draft
  passed any enforced template whose `required_labels` was empty, which is exactly what the create
  dialog produces when the optional classes box is blank; `allow_empty` now makes a blank item
  declarable rather than fallible-into. (2) **`save_draft` had no state guard in the ACTOR** — the
  endpoint read the state and invoked the actor as two steps, so a concurrent submit could land
  between them and a late write put never-reviewed shapes into an accepted task, which publish
  carries into silver. (3) Optional typed attributes were never type-checked (`required` conflated
  presence with type, so an optional `enum` accepted anything while the facet advertised its
  `choices`). (4) `TaskTemplate`/`OutputAttr` swallowed unknown keys, so `enforced:` instead of
  `enforce:` returned 201 with enforcement silently off. (5) The seeder **printed "seeded" after
  its lakehouse half failed**, never checked the release actually MOUNTS the host path it writes,
  waited 300 s on a job that had already failed and then discarded the traceback, and — the one
  the audit rated `missing` outright — **verified nothing**; it now refuses on an unmounted or
  mismatched corpus, dumps job logs on either terminal condition, and ends by asking `rask-search`
  for hits, exiting non-zero if it gets none. (6) `seed_bronze_pages.py` treated any 4xx whose body
  contained the substring "exist" as success — and the catalog renders NOT-FOUND as "… does not
  exist", so a register into a namespace whose create had failed was swallowed and reported as
  "converged"; the test is on the 409 status now, never prose.
- **Recorded, not fixed** (the audit's remaining confirmed findings, all lower severity):
  `publish.py:435` stamps the PROJECT's template rather than each task's CAPTURED one, and invents
  a `template_kind` for projects that never declared a template; `modality`/`kind` are captured,
  stamped and displayed but cross-checked against nothing (an `audio` template can be sent image
  items); shape provenance (`source`/`model_version`/`confidence`) is client-supplied but published
  under a comment asserting it is server-stamped; a client-chosen `task_id` plus `consensus_n > 1`
  can mint replica ids longer than the task routes can address, wedging that project's publish; the
  draft is one document per TASK, not per `(task, author)` as its own section header claims, so
  consensus replicas of one item share a draft. Seeder residue: `_make_world_readable` chmods the
  whole corpus root rather than what the run wrote and does nothing for the re-seed case its header
  claims to cover; the script is still pinned to one release name, one node and fixed local ports;
  and the labeling seed hard-codes fixture-internal doc ids with a `dataset_version` that is already
  wrong.
- **Still open after this wave** (the honest residue): **batch mode** (`runners.jobsUrl` — the
  annotator's batch-labeling submit is still an honest mock); **W3 text spans** (doccano parity —
  needs `char_start`/`char_end` on `Shape` plus a span tool; the review/consensus/publish machine
  needs zero new work); **W4 audio through the loop** (the `AudioViewer` + waveform lane and
  `t_start/t_end` exist, but nothing SENDS audio items); **W5 relations & reading order** (the
  `relation`/`order` tools that make DocQA and reading-order configurations rather than code); and
  **an insid3 image build** (the runner runs from a checkout today; weights are Meta-gated).
  Media's table story is also unfinished: search reads ONE declared `row_table` keyed by the
  identity triple, with no UI to choose tables, no joins on non-identity columns, and no
  declaration for external pointers.

- **Recorded, not fixed (named postures):** the Dapr `/actors/*` invocation surface on the
  annotator trusts its caller (the sidecar) and accepts a caller-supplied `actor` field — true for
  EVERY actor method in the plane, guarded today by pod-network topology, not by the app; an
  estate-level decision (dapr-api-token / netpol tightening / a sidecar-only guard like the
  gateway's lineage rows), not a drive-by. And the actor's adjudicate validates against the task
  INDEX, which can lag a task's own actor — safe direction only because the publish re-reads and
  refuses, and the state-report voiding narrows the window; documented in the actor docstring.

**Still open, named (the residue of this close):**

- **S10 — the canvas draft cutover.** The canvas still saves to the media annotations Lance plane, not
  `PUT /tasks/{id}/draft`; until it lands, shapes drawn on the canvas do not travel into a publish (an
  accepted task with no draft lands as a sentinel row — honest, but empty). Deliberately last, per §8.
- ~~**Send-from-search.**~~ Built in the second wave (search results + atlas toolbar → "Send to
  labeling task…"), with the dataset VERSION captured since the third wave.
- **The publish transport's service identity.** The catalog accepts only dex-issued OIDC bearers; the
  fleet has no client-credentials path, so the drive hand-minted a user token into
  `MEDIA_CATALOG_TOKEN`. Decide: a dex client for the annotator (client-credentials) vs forwarding the
  firing user's bearer (dies with the session — the saga outlives requests). The chart's
  `media.catalogToken` mechanism exists and is unset in the local k3s values.
- **`table_properties` never reach the Lance dataset** — the catalog's create carries them into the
  namespace *declare* and the response echo only, so §7.1's "stamped at create" is not readable off the
  table. (Found by the lance-docs audit; estate-wide, not annotation-specific.)
- **RustFS conditional-PUT is assumed, never verified** — Lance's commit atomicity on S3-compatible
  stores requires put-if-not-exists or an external manifest store; nothing in the repo configures or
  proves either. The shared `annotations` table's concurrent `merge_insert`s are the exposed surface.

---

## C. Carrying a stated reason

### C1 · `TableDetail`'s 60-assignment reset effect *(was #119)*

**What.** `TableDetail.svelte` resets ~60 assignments in an `$effect` where `{#key table}` would do it
structurally.

**Why it is still open, with evidence.** The fix re-instantiates a 1000-line component under 215 e2e tests.
This is not caution for its own sake: an edit to that component during this session **dropped 6 of its 10
history versions** (`missing: 9, 8, 7, 5, 4, 3`) with `svelte-check` reporting 0 errors and 0 warnings. It
is a component that punishes casual edits and needs its own pass with a browser drive, not a tidy-up.

### C2 · The product-works pass *(was #97)*

Ten conditions — annotator loop, runners, one-nav, FGA workbench, create-project, preview, lineage facets,
drawers, registry, gates. Orthogonal to the merge. Its premise is the one worth keeping: *drive the product
as a skeptical first user, not the elements.* (The "lineage facets" condition is the same gap as **E1**
below — one item, two names; close it once.)

### C3 · Lineage track remainder *(was #111)*

Spec-fidelity and Marquez-parity reports are done; Dapr-delivery and gold-finding tests landed in `b43b8ff`.

### ~~C3 · Lineage track remainder~~ **CLOSED 2026-07-28, with evidence**

C3's remaining work was "the gold whole-history JSONB embed". **It is built, and has a dedicated test
file.** The item survived only because it was derived from `VERIFY-LINEAGE-OPENLINEAGE.md`, whose §1
verdict — *"Does the product gold write embed lineage today? **No.**"* — went stale without anyone
re-deriving the backlog entry that cited it. That page now carries a correction banner.

What actually ships (`services/medallion/src/medallion/services/compute.py:43-50`):

- `_LINEAGE_COLUMN = "lineage"`, written as Lance JSON.
- **Every** mover stage stamps it, not gold alone — it is in `_RESTAMPED_COLUMNS`, so each stage
  prepends its own hop to the chain it read off its upstream's cell rather than inheriting the
  parent's provenance verbatim.
- `UpstreamFacts.chain` therefore reaches **back to bronze with no graph query** — the consume-layer
  document is complete on its own (R25b).
- The promotion indexes the JSON path `run_id` as `lineage_run_id_idx`, so a consumer filtering
  `json_get_string(lineage, 'run_id') = …` gets an index rather than a full scan.

Pinned by `tests/unit/test_gold_lineage_column.py` — **16 tests, all passing** — including
`test_the_documents_chain_matches_the_derived_from_edges_the_graph_gets` (the JSONB chain equals the
`DERIVED_FROM` edges the same runs write into AGE, so storage and graph cannot disagree),
`test_the_lineage_column_is_re_stamped_not_inherited`, and
`test_a_run_id_filter_selects_exactly_the_rows_that_run_produced`.

**The lesson worth keeping:** a backlog item that cites a document rather than the code inherits that
document's decay. When closing any remaining item here, re-derive against the tree first.

### C4 · Prod-readiness residuals *(was #86)*

Residuals from the retired `GOAL-production-readiness` tracker. Re-derive against the merged chart rather
than the lance-ns one — several will have been answered by rask's operators.

**Where the enumeration lives:** `ASSESSMENT-2026-07-15.md` §3 is the only in-tree gap-by-gap roll-up
(kept for exactly this reason — historical banner, live enumeration). Verified still open on 2026-07-27:
gap 1 (Dex demo-IdP prod posture — `values-prod.yaml` does not touch dex), gap 5 (OpenBao auto-unseal via
a secrets operator — ESO / bank-vaults; `runbooks/RUNBOOK-oncall.md:63` cites "ASSESSMENT gap #5", and
`OPERATORS.md` §5 row 5 says *verify whether rask already operates one* before adopting), gap 6
(registry-qualified image repos + `imagePullSecrets` — zero hits in `chart/`). Also unnamed anywhere else:
audit-log retention rides the observability store's TTL (`observability.retention`, 14d default) — a
compliance deploy must raise it manually (`API.md` records the caveat).

---

## D. Owner-deferred — not work, decisions already made

| Item | Ruling |
| --- | --- |
| **Settings surface** *(was #112)* — break out auth / authz / audit | Owner: *"keep it as is"* |
| **NATS HA / nack operator + GitOps; query engine** *(was #20)* | Owner-parked. The merge plan's PROPOSED decision 5 holds it parked too, noting rask's JetStream is on but streamless and lance-ns's stream-job is its first real consumer |
| **Models registry MLflow parity** *(was #101)* | Deprioritized until after the product pass |
| **Annotator residuals** *(was #100)* — export serializers (COCO / YOLO / CSV / HF) + managed label taxonomy | Owner to schedule. ⚠️ **The export half is the same service as the merge plan's P7c `exporter`** (ALTO 4.4 first, owner-ruled R4: serialization is a separate microservice, never inside the lakehouse or the movers). COCO/YOLO/CSV/HF become additional projections from gold — new functions in that service, not a second export path. Do not build these twice |
| **Storybook** | Struck for now — rask keeps its own (plan P2 step 3); adopt rask's rather than re-deciding |
| `/lakehouse/data` scaffold, `/lakehouse/admin` orphan | Product decisions, not defects with one right answer |

---

## D2. P7a follow-ups (compute-plane cutover, 2026-07-27)

### D2a · ~~The core-api husk retires with the R6/R20 media wave~~ **CLOSED 2026-07-28, with evidence**

**Closed by the R6/R20 wave (P7b):** `services/core`, `services/core_api`, `services/search_api`
and `services/volumes_api` are deleted; the gateway's core rows AND its `/api` catch-all are gone
(an unmatched `/api/*` now 404s `no upstream` — pinned by
`services/gateway/tests/test_routing.py::test_no_catch_all_since_the_r6_r20_wave`); the chart's
`core-api`/`search-api`/`volumes-api` fleet entries, configmap URL rows, dockerfiles and Makefile
image-list entries are deleted; `ray-api` took the clean `ray` name everywhere external (R20),
then became `compute` on EVERY surface — uv member and import included — at R22 (`import compute`
shadows nothing, so R20's PyPI-shadow exception died with the rename). The S3 object browser was
ported into the media viewer (`viewer/api/v1/endpoints/objects.py`, public `/api/media/object*`,
tests `tests/unit/test_objects_browser.py`) and the lakehouse storage browser re-pointed to it.
The EAD `/catalog/search` endpoint retired with **zero frontend callers**; its re-land is D2d below.

### D2b · The lines FTS surface is dark until the governed lines table lands *(re-anchored 2026-07-28)*

**What.** P7a deleted the indexer; the R6/R20 wave deleted `search_api` itself, so the old
"existing indexed data keeps serving" clause **ended** — there is no lines FTS surface at all right
now (nothing called it: `searchLines`/`searchStats` had zero zone importers). The frozen
`s3://images-batch-search/lines` table is a corpse.
**What closes it.** The P7b gold wave: a **catalog-governed lines table** (line text/geometry/
confidence are `GOLD_CONTRACT_COLUMNS`) + a `DatasetRegistry` descriptor, served at
`/api/media/search?dataset=lines&mode=fts`. Thumb crops ride as a blob column served by the media
blob route — no raw-S3-key proxy gets re-created.

### D2d · The EAD catalog re-lands as a catalog-governed Lance table *(new, 2026-07-28 — the second half of D2a)*

**What.** `scripts/index_catalog.py` + `make catalog-index` are deleted; `scripts/harvest_ead.py`
survives (EAD download only). The `archive_catalog` Lance table at `s3://images-batch-search` is
frozen and unserved.
**What closes it.** An ingest job that writes the EAD table **through the catalog** (governed), plus
a descriptor, so `/api/media/search?dataset=archive_catalog&mode=fts` serves it (media search's
dynamic filterable params cover `archive_code`/`date_*` natively).

### D2e · Warehouse-bucket generalization of the objects browser *(new, 2026-07-28 — the R8 follow-up)*

**What.** The viewer's objects endpoints keep volumes-api's two-bucket `Literal` allowlist
(`images-batch`, `images-batch-alto`). R8 frames the browser as "a lakehouse view of the
warehouse's own buckets".
**What closes it.** Replace the hardcoded pair with a warehouse-derived bucket set (and per-bucket
authz once FGA fronts the browser). Recorded, deliberately not widened in the R6/R20 pass.

### D2c · P7b executes the sealed-runner re-cut this gate only pinned

**What.** `runners/htr` still carries `prefetch_pipeline`/`PrefetchActor`, the S3-diff resumability, and
the `PageLoaderActor`/`AltoWriterActor` endcaps — flagged-D, runner READ-only this gate. The seam they're
replaced by is pinned: mover `stageJob` values knob (`MEDALLION_RAY_ENTRYPOINT`), the gold contract
(`medallion/schemas/htr.py::GOLD_CONTRACT_COLUMNS` + its unit pin), and the `/ingest-iiif` head.
**What closes it.** The P7b gate: the runner CLI grows a `stage` subcommand; layout/lines + transcribe
run as `medallion.bronze`/`medallion.silver` movers; the HTR-cascade e2e (IIIF → bronze → silver →
gold with lineage populated) goes green. *(R23 re-tiered the head: the IIIF harvest lands bronze
directly — there is no raw tier.)*

### D2f · The `/ingest-s3` head route for the second external-raw source family *(new, 2026-07-28 — R23)*

**What.** R23 names TWO external-raw source families: the IIIF Image API (shipped: `/ingest-iiif`) and
external object storage (the ra-hcp pattern). The **adapter seam is landed**:
`medallion/services/s3_harvest.py` (`S3PrefixSource` over `packages/storage`'s provider-agnostic
`storage.S3Source` + `s3_input()` for the `(s3://<bucket>, <prefix>)` OpenLineage input), unit-tested
against moto incl. the bronze blob-v2 landing (`tests/unit/test_s3_harvest.py`).
**Why it is open.** The producer HEAD ROUTE (`POST /ingest-s3`: config for source bucket/prefix
allowlists, token/admin auth, #84 project routing — symmetric with `/ingest-iiif`) is scaffolding-only:
wiring it properly needs the same auth/ceiling/project design pass the IIIF head got, out of the R23
corrective wave's scope.
**What closes it.** The route + settings (`MEDALLION_S3_SOURCE_*`), emitting input=`s3://…` /
output=bronze through the same `/bronze-arrival` seam, with the double-fire pin extended to it.

### D2g · The bronze ingest head's own FGA write gate *(new, 2026-07-28 — R23 collapse residue)*

**What.** The retired raw→bronze mover carried the FGA `can_create_table` self-check for producing
bronze. With the collapse, the bronze write happens in the producer, whose ingest routes are door-gated
(app-token / admin OIDC) but do not self-check a writer rung before the Lance write.
`scripts/seed_medallion_fga.sh` now grants `writer` to `user:service-lance-ray` (the producer identity),
so the model DESCRIBES the intended rung.
**What closes it.** The ingest heads (`/produce`, `/ingest-iiif`, `/ingest-media`) check
`can_create_table` on `namespace:bronze` as `service-lance-ray` when `MEDALLION_FGA_ENABLED` — the same
enforce-not-describe posture the movers keep.

## E. Latent — surfaced by the pre-copy docs audit (2026-07-27), adversarially verified open

These were living only inside reference docs, several anchored to tracker IDs that no longer exist.
Recorded here so the merge cannot lose them; each was verified against the code, not just the doc.

### E1 · OpenLineage "where/why" facets are not captured *(same gap as C2's "lineage facets" condition)*

**What.** `parent` (job hierarchy), `jobDependencies` (why a run waits on another) and `processingEngine`
(Ray version) are in the spec, surfaced by Marquez, and unimplemented here — `LINEAGE.md`'s captured-facets
table omits all three; zero hits in `services/`. Was "Tracked in todo #10b / #12b / #17" in
`event-driven-pipeline.md` — a tracker that no longer exists (`dataQualityAssertions` from that same list
DID land via the quality gate).

**The seam, so it is not rediscovered:** `parent` is already name-reserved in `_RESERVED_RUN_FACETS`
(`services/catalog/core/lineage_emit.py:244`) — but only a rejection test exists, no consumer, and the
docstring at line 240 overstates this. Also unrecorded anywhere durable: ingest handles **RunEvent only**
(no JobEvent/DatasetEvent) — likely deliberate scope, but the scope decision itself was never written down;
decide and record it when this is picked up.

### E2 · Resilience residuals `RESILIENCE.md` carries inline, recorded nowhere else

- The chaos rows (pull-a-service → recover) were driven by hand and never encoded as an automated
  mutating harness (deliberately out of default `make e2e` — they scale shared infra).
- Gap #2's "live check remaining: poison-inject → Dapr `deadLetterTopic` parking" was never driven live
  (only unit tests; the #83 DLQ drive exercised the *outbox* surface, not sidecar parking) and the
  runbook section it pointed at (§6.5) no longer exists after the symptom-first rewrite.
- Honesty-note row 1 — lineage scale-0 → restart-replay under the per-app queue-group components — still
  awaits its one-shot re-verify on a fresh deploy (row 3's was closed 2026-07-06; row 1's never was).
- The bottom-line item "transactional outbox / Ray durable producer belongs to the rask merge" is the
  merge plan's P5 Ray unification — `RESILIENCE.md` is its only other record.

### E3 · Lakekeeper-study adoption backlog, the unshipped remainder *(SYSTEM-SKETCH.md, study wfb25lg74)*

Verified item-by-item against the code; none appear in DECISIONS §9. In priority order:

- **#12 · URL-encode user IDs when serializing to OpenFGA** — subjects are raw-interpolated
  (`f"user:{user}"`, `packages/service-kit/src/service_kit/governed/fga.py`); the study ruled this *mandatory before prod OIDC* if
  subjects can contain `@`/`+`/`:`. OIDC subjects here are emails. Smallest and sharpest of the set.
- **#9 · Versioned authz-model migration** (`ACTIVE_MODEL_VERSION` + idempotent `migrate()`) — was ruled
  "mandatory before the 3-axis model"; the 3-axis model shipped without it.
- **#11 · Reconcile-from-catalog** — additive FGA rebuild + opt-in drift deletion with dry-run; absent
  (the only `reconcile.py` is lineage storage-drift, a different thing).
- **#10 · Split tuple helpers (`tuples.py`) + golden tuple tests** — `grant_on_create` is still one
  inline grant; the FGA contract test is not this.
- **#2 · Vended-response `credentials` vs `config` split** — `expires_at_millis` shipped per-vendor; the
  dict split did not.
- **#3 · `request_id` + actor propagation** — zero hits in `services/`; *possibly* superseded by OTel
  tracing + the audit trail, but nobody ever recorded that verdict — record it or build it.
- **#14 · `/refresh-credentials` + `revalidation_window_ms`** — conditional: only if STS/web-identity
  vending is enabled (the default profile is `mode_b`, which never expires); carry the conditionality.

---

## F. The docs sweep — split in two so it cannot collide *(new, 2026-07-28)*

An external classification (39 agents, every proposed delete adversarially verified) found `docs/` is
**not junk-heavy, it is stale-heavy**: 25 of 35 proposed deletions were killed because the files are
referenced from `zensical.toml` nav, from code docstrings, or from tests. Only 3 files survived as safe
deletes; the real work is ~82 docs needing UPDATE.

Every claim below was re-verified against this tree on 2026-07-28 before being recorded here.

**Why it is split.** A second workstream (the information-architecture goal: grouped sidebar, the
`/lakehouse/data/*` → `/lakehouse/catalog/*` rename, one shell per zone, the storage registry) rewrites
the very things a third of these docs describe. Fixing those docs first means fixing them twice. F1 is
everything disjoint from that work and can start immediately; **F2 is not optional and not dropped** — it
is the same sweep, deferred until the IA goal closes.

### F1 · ~~The collision-free sweep~~ **CLOSED 2026-07-28, with evidence** *(branch `docs/p8-sweep`)*

Executed as specified, with **two corrections to this spec** found during the work:

1. **The fold half of "Folds and layout" was assessed and REJECTED — the pairs are not duplicates.**
   `SYSTEM-SKETCH.md` carries the Lakekeeper diff, the gap register and the adoption backlog that
   **§E3 above depends on by name** ("study wfb25lg74"); `ARCHITECTURE.md` contains none of it, and
   SYSTEM-SKETCH already opens with a banner delegating current-state questions to `ARCHITECTURE.md`.
   `DEPLOY.md` is a lance-ns-on-kind walkthrough (governance + observability drive-throughs);
   `architecture/deployment.md` is rask's k3s/helm/images/CI reference. Same name, different subject.
   Folding either would have destroyed load-bearing content — the same error the adversarial pass
   caught in 25 of 35 proposed deletions, applied to a merge instead of a delete. **The layout half
   shipped:** both runbooks moved to `docs/runbooks/` with all 8 inbound links rewritten.
2. **The gate as originally written was unsatisfiable.** `grep -rn "services/common\|from common\."
   docs/` can never return nothing, because `ARCHITECTURE.md`, `lance-ns-merge.md` and this section
   all *name* the dead path in order to declare it dead. The meaningful gate is the one below.
3. `API.md` claimed **two** nonexistent Makefile targets, not one — `make openapi` as well as
   `make openapi-check`. Adding them is out of a docs-only scope, so both claims were dropped and
   replaced with a warning admonition; the missing drift guard is now tracked as **F3** below.

**Delete (verified zero live references after their nav rows go):**
`docs/MERGE-HANDOFF-PROMPT.md` — inbound refs are exactly `zensical.toml:93` and
`docs/lakehouse/index.md:55`; remove both in the same commit. `docs/architecture/phase2-schema.dbml`
(DBML for the deleted relational control plane) and `docs/architecture/viewer-phase3-plan.md` (a plan for
a service dissolved in June) have **zero** inbound references.

**R19 — `packages/common` and `services/common` are both gone.** 27 citations across 13 docs still point
at `services/common/*` or `from common.X`: `DATA-CONTRACT.md`, `ARCHITECTURE.md`, `DECISIONS.md`,
`COVERAGE.md`, `BENCH-2026-07-22.md`, `OPEN-WORK.md` (§E3 above), `DESIGN-annotation-projects.md`,
`FLOW.md`, `MEDALLION.md`, `SYSTEM-SKETCH.md`, `DEPLOY.md`, `ASSESSMENT-2026-07-15.md`,
`RASK-INTEGRATION.md`, `architecture/lance-ns-merge.md`. The real homes are
`packages/service-kit/src/service_kit/{dapr_publish,control_events,lakehouse/outbox}.py` and
`service_kit/governed/`. `dapr_publish.py:19,61` cites `DATA-CONTRACT.md` back — fix the pair together;
it is the one code file in F1's scope.

**Dead paths.** `deploy/cnpg-age-cluster.yaml` does not exist — it shipped as
`chart/templates/age-cluster.yaml` — and is cited **three** times: `CNPG-AGE.md:40`, `CNPG-AGE.md:73`,
`OPERATORS.md:14`. `API.md:4` claims a `make openapi-check` CI guard that is **not in the Makefile**:
either add the target or drop the claim.

**`ASSESSMENT-2026-07-15.md` is not a delete.** §1–§2 are discharged and describe the dead pre-merge tree,
but §3 is the only in-tree gap-by-gap prod-readiness enumeration and **two** things depend on it —
`OPEN-WORK.md:118` (C4) and `runbooks/RUNBOOK-oncall.md:63` ("ASSESSMENT gap #5"). Cut or hard-banner §1–§2; keep
§3 and both inbound refs intact.

**Folds and layout.** The flat copy created duplicate pairs: `SYSTEM-SKETCH.md` (272L) → `ARCHITECTURE.md`
(359L); `DEPLOY.md` (252L) → `architecture/deployment.md` (206L). And the lance docs sit flat at
`docs/*.md` while rask's site uses subdirs — `RUNBOOK-oncall.md` and `RUNBOOK-restore.md` belong in
`docs/runbooks/` beside the `llm-cluster.md` already there.

**Closed when — both gates green (they are):**

1. Every `zensical.toml` nav target resolves. This is a **regression guard**, not a repair target — it
   was green before the sweep too, so a delete that skips its nav row is what turns it red.
2. No doc cites the dead path *as live*:
   `grep -rn "services/common/\|from common\." docs/ --exclude=OPEN-WORK.md` returns nothing. The
   trailing slash is what makes this meaningful — it matches a **file path**, so the three surviving
   prose mentions (which state the path is gone) correctly do not match.

### F3 · ~~The OpenAPI specs have no drift guard~~ **CLOSED 2026-07-28, with evidence**

**The F1 diagnosis was wrong, and the correction matters.** F1 grepped only the `Makefile`, found no
`openapi` / `openapi-check` targets, and concluded there was no drift guard — so it *removed* API.md's
claim. In fact the guard was fully built and enforced: `scripts/gen_openapi.py` dumps both specs from
the live FastAPI apps, `.dagger/openapi.go` snapshots-regenerates-diffs them, and
`.github/workflows/ci.yml:49` runs `dagger call openapi` on every push. Both files referenced
`make openapi` / `make openapi-check` **by name** in their own comments. Only the two Makefile targets
were ever missing — the docs described a real guard through an entry point nobody had added.

Deleting a true claim because its local entry point was absent is the same failure mode as deleting a
stale-but-referenced doc: **verify the capability, not just the one file you grepped.**

**Landed.** `make openapi` and `make openapi-check` now exist and mirror CI. `uv sync --all-packages`
is load-bearing in both — the root `pyproject.toml` has `dependencies = []`, so a plain `uv run`
installs no workspace member and `import catalog.main` fails with `ModuleNotFoundError`. (Note
`scripts/gen_openapi.py:27` still inserts `services/` on `sys.path`, which predates the src-layout
conversion and no longer resolves anything; it is inert once the packages are installed.)

**It caught a real regression on its first run.** The committed `catalog-openapi.json` had 100 paths;
the live app serves 101. `/v1/user-state/dock-layout` had landed without a spec refresh, so
`dagger call openapi` was already failing on this branch. The spec is refreshed in the same commit.

**Closed when — done:** `make openapi-check` passes locally and matches what CI enforces, and API.md
states the guarantee rather than a warning.

### F5 · ~~The annotator canvas cannot be witnessed locally~~ **CLOSED 2026-07-28, with evidence**

**The blocker was the fixture, not the platform.** A first attempt concluded this was blocked on
**A1** (the corpus lives on a node-local `hostPath`, absent on a dev box). That was the wrong
verdict: `MEDIA_DB_ROOT` / `MEDIA_DESCRIPTOR_DIR` / `MEDIA_DB` are all env-configurable, so a corpus
can be *synthesized* locally instead of waiting for A1 to move the real one.

`scripts/seed_demo_corpus.py` now builds one — one document, one chunk, one rendered page image —
and the full loop was driven in chromium: **a rectangle drawn on the canvas, saved to Lance
(`POST /api/annotations/… 200`), and still present after a reload** (`annotations.lance` 3 → **4
rows, v2**; status bar "4 annotations from Lance").

Four things had to be right, none of them documented anywhere — recorded here because each cost a
debug cycle and the next person will hit all four:

1. The page-image column must be a Lance **blob-v2**, or the registry refuses the whole dataset
   (`document.media_blob is not a lance.blob.v2 column`). Blob-v2 is a **struct**
   `{data, uri}` — raw `large_binary` is rejected — and cannot be written at the default 2.1 file
   format, so `data_storage_version="2.2"` is mandatory.
2. `speech_id` / `chunk_id` must be **integers**. The viewer builds its frame filter with unquoted
   numeric literals, so string columns fail with *"Received literal Int64(0) and could not convert
   to literal of type Utf8"*.
3. A **`frame_idx`** column must exist even for a single-frame chunk — the frames endpoint projects
   it to pick the representative frame.
4. `capabilities` is **declared, not probed**: without `{"frames": "chunks.image"}` in the descriptor
   the dataset lists with `capabilities: []` and the annotator has no images to open.

**Still true, and worth fixing separately:** `scripts/dev-micro.sh` starts `:8101`/`:8804`/`:8820`/
`:8888` but **never `:8103`**, the annotations plane — it had to be started by hand here. And
`/capi/v1/me` 502s without a catalog, which is cosmetic for the canvas but the one console error in
the run.

### F4 · The P7a/P7b dead-name sweep — the *other* cause *(new, 2026-07-28 — surfaced by F1)*

**What.** The classification named **two** systematic causes of staleness. F1 closed the first (R19,
the dead `common` package). This is the second: docs still describing the orchestrator, the
`core_api`/`search_api`/`volumes_api` services, `packages/htr`, or `/default/<zone>` base paths — all
killed by P7a/P7b/R15–R28.

**Why the "82 stale docs" figure is misleading, measured 2026-07-28.** Two exclusions collapse it:

- **`docs/superpowers/**` is not in the published nav** (`grep -c superpowers zensical.toml` → 0).
  Its `plans/` and `specs/` are **dated process artifacts** — a plan written 2026-06-16 correctly
  describes the tree of 2026-06-16, so *rewriting* them would falsify the record.

    **Owner ruling, 2026-07-28: the unreferenced ones were deleted** (34 files — 26 orphans, then 8
    specs that the first pass orphaned by removing the plans that linked them). Git history keeps
    them; they were unpublished and described planes that no longer exist. **The 10 still linked from
    `docs/lakehouse/index.md` are kept, and remain out of scope for content sweeps** — do not update
    them to current state; they are records of what was decided when.
- `lance-ns-merge.md` and this file legitimately name dead things in order to declare them dead.

What remains is **11 nav-served files**, and `architecture/system-overview.md` already carries a
P7a warning banner. Five more — `architecture/frontend-conventions.md`,
`architecture/frontend-microfrontends.md`, `architecture/layout.md`, `components/progress.md`,
`components/ui.md` — belong to **§F2** and wait on the IA goal.

**The actual F4 work-list is six files:** `architecture/microservices.md`, `architecture/deployment.md`,
`architecture/layout.md`, `DECISIONS.md`, `packages/htr.md`, `reference/htr.md`. Note `packages/htr`
is not a package at all — it is the sealed `runners/htr`, outside every workspace glob.

**Closes when.** `grep -rl "core_api\|search_api\|volumes_api\|packages/htr\|/default/" docs/
--exclude-dir=superpowers --exclude=lance-ns-merge.md --exclude=OPEN-WORK.md` returns only files
whose mention is an explicit tombstone, and the nav gate is still green.

### F2 · ~~The deferred remainder~~ **CLOSED 2026-07-28** — the IA goal landed, so this ran

`678e2d5` renamed `/lakehouse/data/*` → `/lakehouse/catalog/*`, which was the thing F2 waited on.

**The `@source` bug is fixed** (`frontend-conventions.md:319,347`): it shipped a copy-pasteable
`@source` with **four** `../` where three is correct, and copying it rendered every `@rask/ui` class
unstyled with no error and no warning. Verified against `frontend/microfrontends/home/src/app.css:7`.

**`frontend-conventions.md` and `frontend-microfrontends.md` are bannered rather than rewritten.**
Their *reasoning* is sound and worth keeping — why rask splits the frontend, why each zone owns a
static base, why dev and prod composition are separate layers sharing only that base. Their
*inventory* is pre-merge: three retired zones, `/default/<domain>` bases, 3 packages where there are
now 8, and a gates section naming ESLint and Prettier. Rewriting the inventory in place would have
produced a second, competing zone list to keep in sync; the banners point at
`.claude/skills/rask-frontend` and `rask-styling`, which are checked against the code and updated
with it. `frontend-conventions.md`'s self-description as "the single source of truth" is the part
that was actively harmful, and the banner sits above it.

<details><summary>Original F2 scope, for the record</summary>

Not dropped — deferred because the IA goal rewrites the subject matter. Pick this up the day that goal
closes; each item names why it waits.

- **`AUTHZ.md`'s per-zone disclosure table** — line 54 tabulates `` `lakehouse/data` ``, the exact path the
  IA goal renames to `/lakehouse/catalog`. The table also lists fewer than the 7 real zones, and R15 makes
  a missing zone a defect — **that applies to the doc too**.
- **The frontend doc cluster** — `architecture/frontend-microfrontends.md` (305L),
  `architecture/frontend-conventions.md` (592L), `architecture/layout.md`, `components/frontends.md` (44L,
  folds into frontend-microfrontends), `components/progress.md` (264L, self-declares "historical", is
  referenced from `frontend-microfrontends.md:305`), and `architecture/frontend-monorepo.md` (34L, folds
  into frontend-conventions). All describe the `AppShell`/`ZoneNav` structure the IA goal replaces.
  One fix is independent of that goal and should ride along: `frontend-conventions.md:319,347` ship a
  copy-pasteable `@source '../../../../packages/ui/dist'` with **four** `../`; three is correct
  (`frontend/microfrontends/home/src/app.css:7`). Copy-pasting it renders every `@rask/ui` class unstyled
  with no error.
- **`API.md`'s path counts** — says 75/24, the committed specs hold **100/29**, and the IA goal's storage
  registry adds more. Prefer deleting the hardcoded numbers in favour of the `make openapi-check` guard
  over correcting a number that will go wrong again.
- **The three viewer/relational tombstones** — `architecture/data-model.md` (132L, its thesis is the dead
  relational batches control plane, but it carries an ER diagram someone may still want),
  `architecture/viewer-design.md` (659L for a dissolved monolith, referenced from `architecture/index.md`
  and `microservices.md`), `projects/viewer.md` (71L, a tombstone for a plane that has itself since died).
  Two independent verifiers disagreed on all three, so they need a judgment call rather than a blind `rm`.
  Whatever is deleted, fix the inbound reference in the same commit.

**Closes when.** The F1 gates still pass, `AUTHZ.md` lists all 7 zones with post-rename paths, no doc
references a `ZoneNav`/shell shape the code no longer has, and each of the three tombstones has been
explicitly kept-with-a-banner or deleted-with-its-referrers-fixed.


</details>

---

## G. The chart provisions RBAC and consumers for things it never installs *(new, 2026-07-29)*

Two independent findings, same shape: a resource type or service is referenced by working code, granted
RBAC, and consumed by the UI — while the chart never creates it. Each presents as an empty list or a
500 far from the cause.

### G1 · No `Project` CRD, so no project can exist

`services/controlplane/src/controlplane/k8s.py:10-13` lists
`group=platform.rask.io, version=v1alpha1, plural=projects`. That CRD is **not installed and not
shipped**:

```
kubectl get crd projects.platform.rask.io   → NotFound
grep -rl platform.rask.io chart/            → only templates/controlplane.yaml (the RBAC)
/api/projects                               → {"detail":"cannot reach kubernetes api"}
```

So the chart grants a ClusterRole over a resource type it never registers. The controlplane pod is
healthy and correctly configured; Kubernetes 404s because the type does not exist. A "Default" project
is not missing — **no project can be created at all.**

The frontend disagrees on purpose and that hides it: the sidebar switcher falls back to
`{ name: 'Default', subtitle: 'Project' }` derived from the request host, so the UI displays a project
the backend has never heard of. Two sources, one placeholder, and the mismatch reads as a display bug.

Fix: ship the CRD (`chart/crds/` or a template) and either seed a default `Project` CR or give the UI
a create path. Until then `/lakehouse/catalog/projects` and the home picker can only be empty.

### ~~G2 · `compute` is built and imported but never deployed~~ **FIXED 2026-07-29**

`.docker/compute.dockerfile` exists, `make k3s-build`/`k3s-import` build and load `compute:dev`, and
the gateway routes `/api/ray` + `/api/serve` to dapr app-id `compute` unconditionally
(`gateway/__init__.py:78,103,105`). But the Deployment renders only under `singleTenant.enabled`
(`chart/templates/fleet.yaml:12`), which defaults false and is set by no shipped path — not
`make k3s-up`, not `values-prod.yaml`, not the Tiltfile.

Result: `/api/ray/*` answers `ERR_DIRECT_INVOKE: failed to resolve compute-dapr…` on every default
install. `Makefile:406` prints that exact route as the post-install check, and `dev-frontends-k3s`
blocks forever polling it.

`compute` is not a single-tenant concept — it is a client for a Ray dashboard URL
(`settings.ray_dashboard_url`), and the Ray cluster is EXTERNAL (`https://dev-kuberay.ra.se`,
reachable). Fix: render it unconditionally the way `controlplane.yaml` already does. **Fixed:** `fleet.yaml`'s gate named `"gateway"` literally; it now reads `$svc.frontDoor`, and both
gateway and compute declare it. `dapr-resiliency.yaml` restated that gate as `singleTenant` alone while
claiming to derive it — so it was corrected in the same pass, or compute would have rendered a pod the
gateway invokes with no timeout, retry or circuit breaker. `rayservice.yaml`
carries the same gate but is genuinely optional — an in-cluster Ray is only wanted for exercising
auth/OpenBao/Dapr locally.


---

## H. Lance performance-guide audit *(new, 2026-08-03)*

The fleet was audited against the Lance performance guide (`lance_docs/guide.md`, "Lance Performance
Guide" — lance.org/guide/performance/). Compliant and deliberately-at-defaults: the FRI
`compact_files(defer_index_remap=True)` recommendation is implemented with a measured fallback
(`services/compaction/src/compaction/services/optimize.py:92`, pinned by
`tests/unit/test_compaction_optimize.py`); AIMD throttle tuning and fragment sizing are untouched
defaults and fine at current scale. Three findings remain open. H1's two halves are **coupled** —
fixing the first without the second converts a latency problem into an OOM-kill problem.

### H1 · Per-request `lance.dataset()` opens everywhere, and Lance's default cache ceilings exceed the pod limits ~17×

**What.** The guide: the metadata cache (1 GiB default) and index cache (6 GiB default) are per
dataset instance — "create a single table and share it across your application", or share a session
across opens. The fleet does the opposite on every path: `lance.dataset(uri)` is opened fresh per
request/operation in the viewer's blob-serving hot path
(`services/viewer/src/viewer/api/v1/endpoints/pages.py:90`), the medallion movers
(`services/medallion/src/medallion/services/compute.py`), lineage reconcile
(`services/lineage/src/lineage/core/reconcile.py`), and the catalog's own open helper
(`services/catalog/src/catalog/core/namespace.py:48`). Every call pays cold manifest fetches and
index loads from RustFS and discards the caches.

**The coupling.** Nothing sets `index_cache_size_bytes`, `io_buffer_size`, `batch_size` or
`LANCE_IO_THREADS` anywhere in the repo, so Lance's ceilings are the defaults: 1 GiB metadata cache
+ 6 GiB index cache + 2 GiB io buffer, 64 cloud IO threads. The Lance services run under the 512 Mi
`resources.default` tier (viewer: 1536 Mi — `chart/values.yaml:226,248`). Today this does not OOM
*because* per-request opens throw the caches away before they grow — the perf bug is acting as the
memory bound. The graph service's measured 512 Mi OOM (2026-07-26, `chart/values.yaml` comment) is
the same class: unbounded `.to_table()` materialisation, which the guide explicitly warns against.

**Why it is open.** A shared dataset handle pins a version, so the fix is not a one-liner: it needs
`checkout_latest` (or a session-scoped open) plus an explicit freshness contract per service. Nothing
in the code marks the current shape as a deliberate freshness-over-latency choice.

**What closes it.** One coupled change per Lance-serving service: a shared session/handle with an
explicit refresh policy, **and** in the same change `index_cache_size_bytes` (+ `io_buffer_size` if
IO threads are raised) sized to the pod's cgroup limit.

### H2 · `LANCE_CPU_THREADS` unset for Lance-under-Ray

**What.** The guide names this exact deployment shape: override the compute pool "when running
multiple Lance processes on the same machine (e.g. when working with tools like Ray)". The
medallion's Ray jobs (`scripts/ray_stage_job.py`, `scripts/ray_iiif_ingest_job.py`,
`scripts/ray_train_job.py`) run Lance inside Ray workers; the submitted `runtime_env`
(`services/medallion/src/medallion/services/ray_submit.py`) passes OTEL vars but no `LANCE_*`, so
each concurrent Lance-using worker sizes its compute pool to all node cores and parallel actors
oversubscribe CPU during decode-heavy stages.

**What closes it.** Set `LANCE_CPU_THREADS` (and matching `LANCE_IO_THREADS`) in the jobs'
`runtime_env` `env_vars`, sized to the actor's CPU allocation. Small and isolated.

### H3 · Lance trace events are invisible to the observability stack

**What.** `LANCE_LOG`/`LANCE_TRACING` sit at defaults (info to stderr — the log pipeline does pick
that up, so nothing is lost). But the guide's trace-event catalogue maps directly onto the
GreptimeDB/RED setup: `lance::events::object_store::throttle` would show RustFS throttling (AIMD
rate drops), and `lance::execution` carries per-query `iops`/`bytes_read`/`parts_loaded` — the
counters the guide says to use for diagnosing index-cache misses (and the measurement H1's fix would
be judged by).

**What closes it.** One env var in the chart's `lance.otelEnv` block
(`chart/templates/_helpers.tpl:608`), e.g. `LANCE_LOG=warn,lance::events=info` — throttle +
execution events surfaced without app-log noise. An opportunity, not a defect.

---

## How this survives

1. **P0** of `docs/architecture/lance-ns-merge.md` copies this file to `rask/docs/OPEN-WORK.md`.
2. **P8** reconciles it — items closed *by* the merge get struck with the evidence; the rest carry forward
   into rask's own tracking, renumbered or not, but never silently dropped.
3. `MERGE-REPIN-DELTA.md` was a diff, was applied (the plan is re-pinned, rulings R8–R10 + D7 recorded),
   and was deleted as its own instructions required — git history keeps it. **This file is not deletable**;
   it is reconciled at P8, never dropped.


---

# Folded-in trackers

## UX reactive evidence

*Was `docs/GOAL-UX-REACTIVE-EVIDENCE.md`.*

## GOAL-UX-REACTIVE — the evidence, in one place

Written 2026-07-27 because the evidence for these twenty conditions kept living in transcript scrollback,
which meant every new context window made it look unproven. It is a *record*, not a claim: every line below
is command output, and every command is named so it can be re-run.

Re-run everything: the commands are inline. Nothing here is asserted; where a fix is claimed, the fix was
broken deliberately and watched to fail first.

!!! note "Pre-merge record — the names below are lance-ns's, not rask's (banner added 2026-07-28)"

    This is a **closed goal's proof-log**, carried into rask deliberately as a comparison baseline
    (`architecture/lance-ns-merge.md:81` — "so a merged-tree regression can be compared against what
    was actually proven"). Its evidence stands *as evidence*; what has moved is every name around it,
    so do not read it as a description of the current tree:

    - It enumerates **four** zones (`/home · /lakehouse · /media · /annotator`); there are now **seven**.
    - Imports read `@repo/*`; the scope is now `@rask/*` (`@repo/zone-contract/poll-reason.test.ts`
      lives at `frontend/packages/zone-contract/src/poll-reason.test.ts`).
    - Pod and release names are `lance-ns-*`.
    - `make frontend-images && make frontend-load` at §7 are **not Makefile targets**, so that run is
      not reproducible as written.

    Kept rather than rewritten: rewriting a proof-log to match a tree it was never run against would
    destroy the only thing it is good for.

---

### 1 — the history endpoint is deployed, not just written

`kubectl exec <catalog pod> -c catalog -- python -c "httpx.get('http://localhost:2333/openapi.json')"`

```
pod: pod/lance-ns-catalog-84874df76c-lbrdr
total paths: 101
history: ['/v1/table/{id}/history']
operationId: table_history_v1_table__id__history_get
```

### 2 — the ingress permits a long-lived stream

`kubectl get ingress -o jsonpath=…`

```
lance-ns-frontend  proxy-read-timeout=3600
```

### 3, 9, 10, 16 — both users, all four zones, the panel scoped to its own dialog

`node scripts/verify_all_zones_both_users.mjs` → **`✓ conditions 9, 10, 16 PROVEN`**

```
✓ alice sees the run bell in /home · /lakehouse · /media · /annotator   — 1 bell(s) each
✓ bob   sees the run bell in /home · /lakehouse · /media · /annotator   — 1 bell(s) each
  /lakehouse/api/runs -> 200, 891 runs, 2 failed
✓ the bell opens a panel with its own role and name
✓ the panel — not the page — says Notifications
✓ a FAILED run is inside the panel, with its error, not just a red badge — maintain: Wrapped error:
✓ failures sort above completions — first state word: Failed
✓ no silently cut text in 12 measured panel rows — 0 cut without a marker
✓ every truncated row carries the full string in title= — 0 unrecoverable
✓ the media zone's bell opens a panel fed by ITS OWN transport
✓ the annotator zone's bell opens a panel fed by ITS OWN transport
  alice → /lakehouse/governance/audit: 200      bob → /lakehouse/governance/audit: 403
✓ bob is refused, and told WHY — 403: "Admin is estate-admin only. These surfaces span every tenant."
✓ and alice, who holds the privilege, gets the surface itself — 200
```

Condition 10's measurement is in the script: element crops at `deviceScaleFactor: 3`, and overflow is
classified rather than eyeballed — a `line-clamp`/ellipsis with a `title` is announced truncation, a bare
overflow is a clipped descender. Two "defects" this caught were **my measurement**, not the product: the
clamped error rows, and a 200-character read of sidebar chrome that hid the words *"Admin is estate-admin
only"*.

### 4 — the timers, and a stated reason for every survivor

```
home 0 · lakehouse 1 · media 1 · annotator 0        (real calls, comments excluded)

lakehouse/src/lib/models/Experiments.svelte:12:   // POLL REASON: a decaying rate has no event…
media/src/lib/service-health.svelte.ts:23:        // POLL REASON: liveness has no event…
```

Enforced by `@repo/zone-contract/poll-reason.test.ts` — a new timer without the marker fails the gate.

### 5 — user work follows the person, not the browser

`node scripts/verify_user_state_browser.mjs` → **`✓ condition 5 PROVEN`**

```
✓ alice sees her new view in the context that saved it — cond5-ms2xy306
✓ the server holds it — HTTP 200
✓ a FRESH browser context shows the view
✓ bob does NOT see alice's view
```

### 6 — the expensive read is cached, shared, and still gated

`node scripts/verify_atlas_cache.mjs` → **`✓ condition 6 PROVEN`**

```
alice cold: 200 miss 6678928B
✓ a repeat read is a hit
✓ a second CALLER is served warm — one fill for everyone allowed to see it — 200 hit 6678928B
✓ an unauthenticated caller is refused WHILE the entry is warm — 401, 29B
✓ and refused on the annotator zone too — gating a URL is not gating a resource — 401, 29B
✓ junk `v` tokens cannot fork the cache — hit,hit,hit,hit,hit
✓ and the product entry survives them — hit
```

### 7 — every gate, cold

```
uv run ruff check services scripts tests          All checks passed!
uv run ruff format --check services scripts tests 368 files already formatted
uvx ty check                                      All checks passed!
uv run pytest tests/unit tests/integration        1213 passed in 30.40s
make openapi                                      no drift
bash scripts/prod_render_check.sh                 ✓ NetworkPolicy=12, OpenFGA=3, Dapr-HA on, PDBs=14,
                                                    spread=7, tiers=3, alerting on, write-cap=2 fits 1Gi,
                                                    rustfs-externalize atomic, ESO path renders
turbo run check test lint fmt:check build --force  Tasks: 43 successful, 43 total · Cached: 0 of 43
turbo run test:e2e                                 home 5 · lakehouse 215 · media 2 · annotator 8
```

**One honest note on the e2e run.** The first attempt reported `16 failed`. Every failure was
`net::ERR_CONNECTION_REFUSED at http://localhost:5294` — I had wiped every `.svelte-kit` for the cold build
while dev servers from the previous run were still alive and being reused (`reuseExistingServer` is on
locally). Killing the stale listener and re-running gave **215 passed, exit 0**. Not a product regression,
and worth recording precisely because "16 failed" is exactly the shape of thing that gets waved away.

### 8 — images rebuilt, pods deleted, digests changed

Zone digests before → after a `make frontend-images && make frontend-load` + `kubectl delete pods`:

```
annotator  33bd12c2 → 4d6f2d90 → 957fb0f4
home       ac8a88d1 → 0aa2e0f6 → a163b388
lakehouse  fbcc366b → 50869fd3 → f7ff40f4
media      598d1eed → 28e69448 → b7368c16     (28e69448 = the id `kind load` reported loading)
```

Read **by container name**, never `containerStatuses[0]` — index 0 is the daprd sidecar on a 2/2 pod, and
its digest is identical across every service. `tests/unit/test_invariants.py` now forbids index access.

### 11 — the svelte MCP autofixer, per touched component

26 `.svelte` files changed since the goal was set. All 26 through `mcp__svelte__svelte-autofixer` at
`desired_svelte_version: 5`. **Twenty returned `{"issues":[],"suggestions":[]}`. Zero issues across all 26.**
Six returned suggestions; each judged in the goal tracker (retired — git history). One was a real defect — `saved-views` called
`load()` inside an `$effect` whose guard reads state `load()` assigns, so two components mounting in the
same tick each issued a full GET. Fixed with an in-flight promise; broken deliberately →
`AssertionError: expected 3 to be 1`.

### 12 — the ledger is current

The parent tracker (`GOAL-VERIFY-PULL.md`) was rewritten row by row against the day's evidence, then
retired with the goal on 2026-07-27 (git history). Open work moved to `OPEN-WORK.md`; the merge state
lives in the rask plan.

### 13 — every open task disposed of

The disposition table — 18 tasks, each done-with-evidence or struck with a stated
reason — lived in the retired goal tracker (git history). The still-open items carry in `OPEN-WORK.md` so they survive the merge.

### 14 — the recorded mistakes have guards, and the guards were broken on purpose

```
=== guard 1: reintroduce | default 1 ===
E   chart/templates/gateway.yaml:92: replicas: {{ .Values.gateway.replicas | default 1 }}
=== guard 2: reintroduce containerStatuses[0] ===
E   scripts/ray_e2e_stack.sh:125: … jsonpath='{.items[0].status.containerStatuses[0].imageID}'
=== guard 3: drop auth.secretStore from the state store ===
E   AssertionError: component lance-statestore uses secretKeyRef with no auth.secretStore, so Dapr
    resolves it from a Kubernetes Secret instead of OpenBao
```

Restored → `pytest tests/unit/test_invariants.py` **34 passed**. Guard 1 then found a live instance of its
own class nobody was looking for: nine `replicas: {{ … | default 1 }}` sites rendering `1` for an explicit
`0`. Proven both ways — `AFTER gateway Deployment -> replicas: 0`, `BEFORE … replicas: 1`, rest of the
render byte-identical.

Two more guards were added later and also broken first:
`notification-surface.test.ts` (*"annotator's root layout renders AppShell WITHOUT a notifications feed"*)
and `no-networkidle.test.ts`. Restored → **591 passed**.

### 15 — a live stream past 255s with no reconnect

`HOLD_S=270 node scripts/verify_live_stream_timeout.mjs`

```
→ alice signs in and opens /lakehouse/admin/events (holding 270s, clearing the 255s bar)
  #1 opened at t+0.8s, STILL OPEN after 270.0s
  #2 opened at t+0.8s, STILL OPEN after 270.0s
✓ no stream was severed during 270s — 2 opened, 0 closed
✓ the live stream survived past 255s
```

The script used to print "past a 60s nginx default" whatever the hold, so a long run proved the harder bar
while labelling itself with the easier one. It now names the bar it actually clears. **Evidence that
mislabels itself is not evidence.**

### 17 — no session-gated 200 says `Cache-Control: public`

`bunx vitest run src/server-cache.test.ts` → **21 passed**, including *"never replays `public` on a response
this route gated behind a session"*. Broken deliberately (the rewrite disabled):

```
AssertionError: expected 'public, max-age=300' to be 'private, max-age=300'   ×2
AssertionError: expected 'max-age=120'        to be 'private, max-age=120'
```

### 18 — the rows/bytes column is honest, never a misleading 0

`bunx playwright test e2e/data/table-history.spec.ts` → **24 passed**, including
*"rows/bytes are shown only where the writer measured them (#113)"* and *"an empty author and a missing run
both read as an explicit dash"*. The empty cell renders `—` with
`title="the run that wrote this version measured no output statistics"`.

### 19 — the data-loss path is closed by a test that fails without the fix

`uv run pytest tests/unit/test_user_state.py` → **35 passed**. With `get()` returning `None` again instead
of raising:

```
FAILED tests/unit/test_user_state.py::test_an_unparseable_record_is_unreadable_not_absent
1 failed, 34 passed
```

Client half: `user-state.test.ts` + `saved-views-store.svelte.test.ts` → 11 passed.

### 20 — workflows use `pipeline()` unless a barrier is justified

```
ux-reactive-track:            parallel=0 pipeline=1     (the one that cost ~40 minutes; rewritten)
discharge-owner-decisions:    parallel=0 pipeline=1
rask-docs-zone-set-sweep:     parallel=0 pipeline=1
frontend-state-architecture:  parallel=1 pipeline=0     ← justified barrier
```

The survivor fans four design agents into a **single** judge that scores them against each other — stage N
genuinely needs all of stage N−1, which is the test the rule turns on.

---

### The closing note worth keeping

Every one of these was green before the drive that found the real defect. The notification bell was shared,
tested and shipped — in **one zone out of four**. Two adversarial passes returned **4/4 REFUTED** on claims
already pushed and green, including an anonymous 6.6 MB read reachable in two zones. Main was red for five
CI runs on two causes invisible to every local gate.

Twenty conditions met is a floor, not a finish.


## Lineage / OpenLineage verification

*Was `docs/VERIFY-LINEAGE-OPENLINEAGE.md`.*

## Verify: lineage / OpenLineage

Evidence log for the lineage track. Each section states a claim, the command that tested it, and the
verdict. Live checks run against the kind cluster `lance` (helm release `lance-ns`).

!!! danger "The §1 verdict below is OUT OF DATE — gold DOES embed lineage now (re-checked 2026-07-28)"

    The table immediately below answers *"Does the product gold write embed lineage today?"* with
    **No**. That is no longer true. `services/medallion/src/medallion/services/compute.py:43-50`
    defines `_LINEAGE_COLUMN = "lineage"`, stamps it on **every** stage via `_RESTAMPED_COLUMNS`, and
    builds a `lineage_run_id_idx` index on the JSON path `run_id` — i.e. exactly the follow-up this
    document filed as unbuilt. `UpstreamFacts.chain` then reads the upstream `lineage` cell to walk
    provenance back to bronze without a round-trip (R25b).

    **This matters beyond the doc:** `OPEN-WORK.md` C3 still lists "the gold whole-history JSONB
    embed" as the lineage track's remaining work, citing this page. That item needs re-deriving
    against the code rather than against this verdict.

    Two other things here have rotted with the merge and are **not** to be copied: the module paths
    (`common.schema.type_label`, `common.lancekit.openlineage._type_label`, `common.openlineage.run_id_for`)
    moved to `packages/service-kit/src/service_kit/lakehouse/schema.py` and `.../lancekit/openlineage.py`
    at R19; and every reproduce command targets the retired kind cluster / `lance-ns` release.

    The *method* — claim, command, verdict — is why this page is kept rather than deleted. Re-run it
    before trusting any individual row.

### Gold: lineage as JSON in the Lance file

**The claim under test** (from `docs/LINEAGE.md`, `services/lineage/seed.py`, and the medallion demo
header): *gold embeds its whole upstream provenance as a JSONB `lineage` column inside the Lance file.*

#### Verdict

| Question | Answer |
| --- | --- |
| Does the **product** gold write embed lineage today? | **No.** The cascade's silver→gold mover writes `id, payload, source_rowid, stage` and nothing else. |
| Where does the JSONB embedding actually live? | Only in `scripts/medallion_demo.py::write_gold` (the demo driver) and in `services/lineage/seed.py`'s *synthetic* schema facet. Its reader, `GET /demo/datasets`, is **off** on the cluster (`LINEAGE_DEMO_DATA_ENABLED=false`). |
| Is the JSONB-in-Lance representation still what Lance recommends? | **Yes** — `pa.json_()` is the current recommendation and it is a *stronger* choice than we knew: it is indexable. Our demo code already writes `pa.json_()`, so there is no migration debt. |
| Does gold lie about its provenance? | **No.** It says nothing, and what the lineage graph says about it matches storage exactly (below). |

So the claim is **stale, not false in a dangerous way**: nobody is reading a lineage column that does not
exist, because the only reader is disabled. The honest statement is "the demo embeds provenance in gold;
the governed cascade does not".

#### (a) What Lance recommends now — `lance.org/guide/json`

Fetched 2026-07-26; the vendored copy at `lance_docs/guide.md` (`FILE: docs/src/guide/json.md`) matches.

> Lance stores JSON data internally as JSONB (binary JSON) using the `lance.json` extension type. This
> provides: efficient storage through binary encoding; fast query performance for nested field access;
> compatibility with Apache Arrow's JSON type.

The write recipe is `pa.array([json.dumps(doc)], type=pa.json_())`. Query functions: `json_extract`,
`json_get`, `json_get_string/int/float/bool`, `json_exists`, `json_array_contains`, `json_array_length`.
Indexing (the part that is new relative to "store a JSON string"):

> For `pa.json_()` columns, create a scalar index with `IndexConfig` and specify the JSON path to index.
> The query should use the same path literal that was indexed.

and, for text search over the whole document, an `INVERTED` index on the JSON column.

The guide is explicit that a Utf8 column is not equivalent: *"For `pa.json_()` columns, use the JSON index
shown above and query with `json_get_*` or `json_extract`."*

#### (b) What the gold write path actually writes today

The gold writer is `medallion.services.compute.transform_stage`, called from
`medallion.services.transform.handle_stage`. It carries upstream columns forward, mints/carries
`source_rowid`, stamps `stage`, and writes `mode="overwrite"`, `data_storage_version="2.2"`,
`enable_stable_row_ids=True`. There is **no** lineage column anywhere in that path — provenance leaves the
mover as an OpenLineage `RunEvent` (`medallion.schemas.events.build_run_event`), not as data.

Live proof. The terminal mover's own settings name the tenant gold table
(`MEDALLION_TO_DATASET=gold$catalog`, `MEDALLION_GOLD_WAREHOUSE_ENABLED=true`), which resolves to the
project's gold serving warehouse — bucket `acme-gold`:

```
$ kubectl exec deploy/lance-ns-silver-to-gold -c mover -- python -c '<open the dataset with the
  mover's own settings + OpenBao secret>'
=== s3://acme-gold/medallion/gold v 1 rows 8
id: int64
payload: string
source_rowid: uint64
stage: string
 ROW0 id = 0
 ROW0 payload = event-0
 ROW0 source_rowid = 24
 ROW0 stage = gold
```

The default (non-tenant) root is the same shape, minus `source_rowid` on that older incarnation:

```
URI s3://lance-catalog/medallion/gold
version 61 rows 8
id: int64
payload: string
stage: string
ROW0 id = 0 | payload = event-0 | stage = gold
```

A sweep of every Lance table in the `lance-catalog` bucket found no JSON column at all:

```
scanned 124 tables; json columns found: 0
```

#### (c) Judgement — representation and queryability

Measured on the deployed runtime (`pylance 8.0.0`, `pyarrow 24.0.0`), writing a realistic gold provenance
document into a `lineage` column:

```
arrow type written: extension<arrow.json>
lance schema: id: int64
lineage: extension<arrow.json>
--- json_get_string filter        rows matched: 8
--- json_extract filter           rows matched: 8
--- json scalar index
indices: [{'name': 'lineage_idx', 'type': 'Json', ... 'fields': ['lineage'] ...}]
rows matched with index: 8
LanceRead: uri=..., full_filter=json_get_string(lineage, Utf8("dataset")) = Utf8("gold$catalog"), refine_filter=--
  ScalarIndexQuery: query=[Json(lineage = gold$catalog->dataset)]@lineage_idx(BTree)
```

So: **yes, a lineage field can be filtered without a full scan.** The plan resolves the predicate through
`ScalarIndexQuery ... @lineage_idx(BTree)` with an empty `refine_filter` — the JSON column is not scanned.

The same experiment against a plain `pa.string()` column fails on both counts:

```
STRING FILTER ERROR: ValueError Invalid user input: Error during planning: Failed to coerce arguments to
  satisfy a call to 'json_get_string' function: coercion from Utf8, Utf8 to the signature
  Exact([LargeBinary, Utf8]) failed
STRING INDEX ERROR: ValueError Invalid user input: A JSON index can only be created on a Binary or
  LargeBinary field.
```

**No migration cost.** We are not on an old pattern: the demo already writes `pa.json_()`, which is exactly
the current recommendation, and Lance reads it back as `extension<arrow.json>` (value returned as JSON
text; JSONB canonicalises key order). What is *unused* is the indexing half — if gold ever embeds lineage
for real, add `create_scalar_index(..., IndexConfig(index_type="json", parameters={"target_index_type":
"btree", "path": "<field>"}))` and query with the same path literal, or the column is a full scan.

Two follow-ups fall out of this (both outside the medallion partition, filed here so they are not lost):

1. `scripts/medallion_demo.py::write_gold` catches `AttributeError / ArrowNotImplementedError / TypeError`
   around `pa.json_()` and silently falls back to `pa.string()`. Per the errors above, that fallback
   produces a `lineage` column on which **every** JSON function and the JSON index fail — a silently
   unqueryable column. pyarrow is pinned to 24.0.0 in `uv.lock` and `pa.json_()` works there, so the
   fallback is currently dead code; it should either fail loudly or be dropped.
2. `services/lineage/seed.py` declares gold's column as type `"json"` in its schema facet. Until this pass
   the real renderer disagreed — see below.

#### Fix landed: a JSON column is labelled `json`, not `extension<arrow.json>`

`common.schema.type_label` exists to keep raw pyarrow reprs out of the lineage graph (blob → `"blob"`,
vector → `"array<float>"`, binary → `"binary"`). It had no JSON branch, so a JSON column reached the
`SchemaDatasetFacet` — and the frontend column list — as:

```
type_label: extension<arrow.json>
facet_fields: [{'name': 'id', 'type': 'int64'}, {'name': 'lineage', 'type': 'extension<arrow.json>'}]
```

which contradicts the `("lineage", "json")` label `services/lineage/seed.py` already emits for the same
column. This is not hypothetical for the merged media path: `packages/ratch/model/schema.py` writes
`pa.field("alignments_json", pa.json_())` and `packages/ratch/features/topic_tree.py` writes `hierarchy` the
same way, and those tables are emitted through the vendored mirror
`common.lancekit.openlineage._type_label`, which had the same gap.

Both labellers now return `"json"`, matched by
`tests/unit/test_lineage_schema_facet.py::test_type_label_renders_a_json_column_as_json` and
`::test_lancekit_mirror_labels_json_the_same_way` (the mirror test asserts both modules produce the
identical facet, so they cannot drift apart again). Detection is on the extension name
(`arrow.json` / `lance.json`) because pyarrow 24 ships no `pa.types.is_json`.

#### (d) Is gold's provenance consistent with the lineage service?

Nothing is embedded in gold, so there is no embedded copy to diverge. The available check is the graph's
record of the run that wrote gold versus the bytes on storage — and it is exact.

AGE (`lance-ns-age-0`, graph `lineage`), newest `aggregate_gold` run for `acme-gold$catalog`:

```
$ kubectl exec lance-ns-age-0 -- psql -U lance -d lineage -c "... MATCH (r:Run)-[w:WROTE]->
    (d:Dataset {name:'acme-gold$catalog'}) RETURN r, w ..."
Run   {"job": "lance-medallion/aggregate_gold", "author": "analyst",
       "run_id": "9e5d933a-3a8e-5ce8-9cd1-e263afd55d2b", "operation": "aggregate_gold",
       "event_type": "COMPLETE", "event_time": "2026-07-24T17:00:02.648458+00:00"}
WROTE {"version": "1", "row_count": 8, "size_bytes": 284,
       "schema": "[{\"name\":\"id\",\"type\":\"int64\"},{\"name\":\"payload\",\"type\":\"string\"},
                   {\"name\":\"source_rowid\",\"type\":\"uint64\"},{\"name\":\"stage\",\"type\":\"string\"}]"}
```

Inputs and the dataset-level edge:

```
MATCH (r:Run {run_id:'9e5d933a-...'})-[:READ]->(i:Dataset)      -> "acme-silver$features"
MATCH (d:Dataset {name:'acme-gold$catalog'})-[:DERIVED_FROM]->(u) -> "acme-silver$features"
```

Storage, measured with the mover's own `compute.measure`:

```
version 1 rows 8 bytes 284
fields: id:int64, payload:string, source_rowid:uint64, stage:string
```

Version, row count, byte count and the full schema all match the `WROTE` edge; the single input matches
the mover's configured `MEDALLION_FROM_DATASET` chain (`acme-silver$features` → `acme-gold$catalog`).
**No divergence.**

One structural caveat for whoever revives the embedding: in `scripts/medallion_demo.py` the Lance write
(`_perform`) runs *before* the terminal `COMPLETE` is emitted (`_emit_step`), and `_gold_provenance`
builds `produced_by` as a hand-written `{"job", "author"}` dict. The embedded document therefore carries
**no `run_id` for gold's own run** and cannot be joined back to the lineage service by run id — the very
correlation key (d) asks about. If gold is to embed provenance for real, the run id must be computed
first (it is deterministic — `common.openlineage.run_id_for(f"{project}-{operation}-{token}")`) and
written into the document. The DAG direction the demo appends is correct:
`GraphEdge` is documented as "`source` is derived from `target`", and it appends
`{"from": "gold$catalog", "to": "silver$features"}`.

#### Reproduce

```bash
export PATH="$PATH:$PWD/.localbin"
POD=$(kubectl get pod -l app.kubernetes.io/component=silver-to-gold -o jsonpath='{.items[0].metadata.name}')

## gold's real schema + one row (uses the mover's own settings + OpenBao secret)
kubectl exec "$POD" -c mover -- python -c "
from medallion.core.config import MedallionSettings, apply_dapr_secrets
import lance
s = MedallionSettings(); apply_dapr_secrets(s)
ds = lance.dataset('s3://acme-gold/medallion/gold', storage_options=s.storage_options())
print(ds.version, ds.count_rows()); print(ds.schema); print(ds.to_table(limit=1).to_pylist()[0])"

## what the graph holds for the same dataset
kubectl exec lance-ns-age-0 -- psql -U lance -d lineage -t -A -c \
  "LOAD 'age'; SET search_path=ag_catalog,\"\$user\",public;
   SELECT * FROM cypher('lineage', \$\$ MATCH (r:Run)-[w:WROTE]->(d:Dataset {name:'acme-gold\$catalog'})
   RETURN r, w \$\$) as (r agtype, w agtype);"
```


## rask integration

*Was `docs/RASK-INTEGRATION.md`.*

## Merging the lakehouse into rask — integration checklist

This repo's deliverable is **contributed into the sibling `rask/` repo**, not shipped standalone. This is the
concrete migration plan: what folds in, what externalizes to rask's operators, the lance-ray seam contract,
and what to drop. Grounded in rask's actual chart (`rask/chart/`) + this repo's services + chart.

### The boundary (what moves vs what rask supplies)

**We bring (the unit that merges):**
- The **lakehouse catalog** (`services/catalog`, a thin REST adapter over native pylance `DirectoryNamespace`) + the in-process `dataplane`.
- The **lineage estate** (`services/lineage` → Apache AGE graph; OpenLineage; `/reconcile`; column-level + the gold whole-history JSONB). **rask has ZERO lineage** — this is the single biggest net-new capability we add.
- The **OpenFGA WIRING** (`packages/service-kit/src/service_kit/governed/auth/model.fga` + `services/catalog/api/fga_deps.py` + credential vending). **rask provisions OpenFGA but never wires it into any service** — we bring the actual ReBAC enforcement.
- The **event-driven medallion estate** (`services/medallion` producer + movers, `services/compaction`) on Dapr pub/sub over NATS JetStream.

**rask supplies (use, do NOT rebuild):**
- **CloudNativePG** — the Postgres `Cluster` (`<release>-postgres`).
- **rustfs-operator** — the S3 `Tenant` (`<release>-rustfs`, with a `buckets:` list).
- **KubeRay + Kueue** — the Ray cluster (`RayService`) + job admission, and the `ray-kit` / orchestrator submission path.
- **Traefik Ingress** + the **Alembic migration Job** + **GreptimeDB/Vector/Perses** observability + **NATS** + **Dapr**.
- ⚠️ **NOT the frontend, any more.** This line used to read "Frontends (SvelteKit microfrontends)", which
  contradicted §5 in the same document. `apps/web` was retired in the P5 migration and **the four zones ARE
  the frontend** — home / lakehouse / media / annotator, in rask's exact Turborepo shape. They graft into
  `rask/microfrontends/`; nothing of ours is dropped here. See §5.

### Pre-flight (rask already has these — no action)
NATS, Dapr, OpenFGA (server), CloudNativePG, rustfs-operator, KubeRay, Kueue, GreptimeDB stack, Traefik. The
chart pattern is identical (umbrella + `*.enabled` subcharts + externalize-in-prod), so the fold-in is values
+ templates, not a new paradigm. Which of these operators we lean on first, in what order, and why no custom
lance-ns operator is ever built: [`OPERATORS.md`](OPERATORS.md) (also pins the submit-seam boundary — the
agnostic Jobs-REST seam stays in lance-ns; rask supplies the `RayJob`-CR transport behind the same
function signatures).

### Migration checklist

#### 1. Stateful stores → rask's operators (via the P1 externalization hooks)
The externalization hooks added in this repo make this a **values flip**, not a code change:

| lance-ns value | Set to | Points at rask's |
|---|---|---|
| `rustfs.enabled` | `false` | — (drop the hand-rolled Deployment) |
| `rustfs.externalEndpoint` | `http://<release>-rustfs:<port>` | rustfs-operator `Tenant` |
| `age.enabled` | `false` | — (drop the hand-rolled StatefulSet) |
| `age.externalHost` | `<release>-postgres-rw` | CNPG `Cluster` (rw service) |
| `openfga.datastore.uri` | `postgres://…@<release>-postgres-rw:5432/openfga…` | CNPG |
| `observability.externalOtlpEndpoint` | rask's GreptimeDB OTLP | shared observability |
| `stateStore.*` (**new**) | DSN → `<release>-postgres-rw` via the Dapr secret store | CNPG |

⚠️ **The state store did not exist when this table was written.** `lance-statestore` is a Dapr
`state.postgresql` component with `actorStateStore: "true"`, pointed at the AGE Postgres today and resolving
its DSN from OpenBao through `lance-secrets` — never a k8s Secret. On rask it moves to CNPG like the rest,
and its `scopes` must list every app that owns operational state (today: catalog, annotator). An app outside
`scopes` gets "component not found" from its sidecar and every user's saved work 503s, which the sidecar logs
and nothing else notices — `tests/unit/test_invariants.py` pins the agreement.

- **Add the buckets** to rask's `rustfs.buckets`: the lakehouse (`lance-catalog`) + observability (`lance-observability`).
- **Add the databases** to CNPG: `lineage` + `openfga`. **AGE caveat — DECIDED and proven, 2026-07.** AGE reached PG18 (v1.7.0), so it mounts as a CNPG
  **ImageVolume extension on a STOCK image** — option (a) without a custom Postgres build. Proven end to end
  on a throwaway kind cluster with the real CNPG operator (`docs/CNPG-AGE.md`, `.docker/cnpg-age-ext.dockerfile`).
  The CSI-mount leg needs K8s 1.33+. No Lance-native-graph rewrite is required.

#### 2. lance-ray → a real Ray Data job (the one in-scope gap)
Today `services/medallion/producer.py` + the movers are **dummy Ray jobs** — pure lineage emitters by
default, but with `medallion.compute=true` (the B1 toggle) each stage does a real in-process Lance
read→transform→write, so the cascade already produces versioned data, not just provenance. On rask
they become **real Ray Data jobs on KubeRay** (Kueue-admitted, `ray-kit`/orchestrator-submitted). The seam
contract they must honor (so they drop in with no rewiring) — see **§ lance-ray seam contract** below.

#### 3. Wire OpenFGA into rask
rask provisions OpenFGA but doesn't enforce it. Contribute `model.fga` + `fga_deps` so rask's services check
ReBAC (and so the medallion `can_promote`/`can_create_table` gates fire). This also gives rask its first authz.

#### 4. Secrets — align the two-tier model
Map this repo's OpenBao + external-secrets two-tier model onto rask's secret approach (rask uses
`existingSecret` for prod). Keep: app tier consumes via Dapr secret store (sole source); infra tier via
`secretKeyRef` populated by external-secrets from Vault. lance-ray uses **workload identity** (no durable
secret).

#### 5. Drop the demo scaffolding (rask supersedes it)
- `chart/templates/{age-postgres,rustfs,backup-pg,backup-snapshot}.yaml` → CNPG / rustfs-operator.
- `frontend/` + the zone Deployments + `gateway.yaml` → rask's SvelteKit frontends + Traefik Ingress.
  **Grafted-shape (P5, 2026-07-22):** `frontend/` is now a Turborepo + bun workspace in rask's exact shape —
  the 4 `microfrontends/<zone>` apps (home/lakehouse/media/annotator) on the shared `@repo/ui` design
  system + the `@repo/api` seam (the old single `apps/web` app + `@repo/ui` were retired in P5) — so folding
  in is a directory graft of the zones into `rask/microfrontends/`, not untangling a monolith.
- `openbao` dev-mode + the dev `infra-credentials` static Secret → external-secrets from rask's Vault.
- The `dex` demo IdP → rask's real IdP (or keep for local-only).

#### 6. The media plane — absent from every earlier version of this document

`services/{viewer,search,annotator}` merged in under #91 (`docs/LANCE_NS_HANDOFF.md`) and this checklist has
never mentioned them. They are part of the unit that merges, and one of them carries the merge's sharpest
edge:

- **The corpus is a node `hostPath`** (`/var/media-corpus` on this kind node), not a governed table. That is
  fine on a single-node dev cluster and **will not survive a move to rask's cluster** — a hostPath binds a
  pod to a node that happens to hold the data. This is #103 ("media plane on the governed warehouse: corpus
  as registered project tables"), which is deferred today and becomes **blocking at the merge**. Decide
  between: register the corpus as project tables on the governed warehouse (the intended shape), or give
  rask a PVC/object-store path for it.
- **The viewer needs its memory tier.** It was OOM-killed serving thumbnails and now runs 1536Mi/768Mi,
  sized from a measured 955Mi cgroup peak. Carry the tier, not the default.
- **The encoders are URLs, not Deployments** (`encoders.*Url`). This cluster has no `nvidia.com/gpu` in node
  capacity, so vector/hybrid/rerank render disabled with the reason. If rask has GPUs, the same values point
  at real servers with no code change — the wiring is already proven to flip 503 → 200.

#### 7. Live streams need the ingress to permit them — on Traefik, not nginx

Every zone's shell now holds a `query.live` SSE stream open for the run-notification bell. Proven here at
**269.6s with 0 streams severed**, past both nginx's 60s default and Bun's 255s `IDLE_TIMEOUT`. That rests on
two things, and only the second travels:

- `nginx.ingress.kubernetes.io/proxy-read-timeout: 3600` on our Ingress — **rask uses Traefik**, so this needs
  its equivalent (a `ServersTransport` / `responseForwarding` setting) or every zone reconnects on a timer and
  each reconnect re-primes the event window and writes an audit record.
- The application-level keepalive in `@repo/api/runs-feed`, which re-yields the last pulse every 20s. That is
  ours and moves with the code.

`scripts/verify_live_stream_timeout.mjs` takes `HOLD_S`; run it past 255 against rask's ingress to confirm.

### lance-ray seam contract (so the real job drops in)
The dummy producer/movers define the contract the real Ray Data jobs must reproduce **exactly**:

- **Producer (head):** write the BRONZE Lance dataset directly (R23 — raw is the external world; bronze
  is the first governed tier), then **publish ONE OpenLineage run event** to the Dapr pubsub
  `lineage-pubsub` / topic `lineage.events.v1` — `inputs=[<external source: iiif://… or s3://…>]` →
  `outputs=[bronze$events / bronze$pages]`, the `WROTE` edge carrying the **Lance version** facet
  (`DatasetVersionDatasetFacet`). **That is all the real Ray job does.**
  ⚠️ **It must NOT publish `medallion.bronze` itself** — post-B2 the deployed lance-ray app *subscribes*
  to the lineage topic (`/bronze-arrival`) and publishes the first `medallion.bronze` trigger when it
  sees a bronze write. A job that also published `medallion.bronze` would **double-fire the cascade**.
  The head is event-driven: emit the bronze-write event; the arrival subscription does the triggering.
- **Each mover:** subscribe to its upstream trigger → transform (read the from-stage Lance version-range as a
  CDF, write the to-stage) → emit the **`DERIVED_FROM`** OpenLineage edge → publish the next trigger.
- **Gold mover (terminal):** write the gold dataset **with the embedded `lineage` JSONB column** (per
  `scripts/medallion_demo.py: write_gold`) → no next trigger. This is the durable, exportable artifact.
- **Authz:** when `MEDALLION_FGA_ENABLED`, the mover checks `can_create_table` (writer) / `can_promote`
  (validator, silver→gold) as its **service identity** before emitting; unauthorized → `DROP`.
- **Creds:** the job authenticates with **workload identity** (KubeRay projected SA / OIDC token) and vends
  short-TTL, table-scoped creds via the catalog `POST /v1/table/{id}/credentials` (web_identity flow). **No
  durable secret on compute.**

Reproduce those four behaviors in the Ray Data job and the cascade keeps working unchanged.

### Verification (the merge is correct when…)
- `scripts/governance_e2e.sh` + the medallion e2e run **against rask's CNPG + rustfs-operator stores** (not the
  in-cluster hand-rolled ones) and stay green.
- `tests/integration/test_spec_conformance.py` still passes (catalog surface intact).
- A real lance-ray run produces a **gold dataset with the JSONB lineage** + `/reconcile` returns `in_sync`
  against the on-disk Lance version.
- `helm template` of rask's chart shows the catalog/lineage/medallion/compaction workloads pointing at
  `<release>-postgres` / `<release>-rustfs`, with **no in-cluster DNS leaks** and **no plaintext secrets**.

### Open decisions (resolve before/early in the merge)
1. **AGE on CNPG** — custom AGE image vs separate operand vs Lance-native graph (`docs/DECISIONS.md` #age-on-cnpg-vs-lance-native-graph-the-lineage-store-decision). Affects §1.
2. ~~**Tenancy**~~ — **overtaken by shipped work, no longer a decision.** This read "the repo is
   single-warehouse; confirm one warehouse-per-deploy stays the model". It is not: `chart/values.yaml`'s
   `#3-A per-warehouse physical multi-tenancy` provisions a physically separate bucket per warehouse and
   binds top-level namespaces to it (Lakekeeper parity, #27), and #84 added per-tenant medallion zones with a
   project-level policy default. rask's single implicit `default` project is the side that has to widen —
   its services would sit under one project in our model, which is the degenerate case and works unchanged.
3. **Catalog 501s** — **confirmed 7** against `docs/COVERAGE.md` (47/54 backed). A crude `grep -c 501` over
   `services/catalog` reads 8 and is wrong — it counts prose. The 7 genuinely backend-stubbed ops (`docs/COVERAGE.md`: rename / backfill /
   alter_transaction / MV create+refresh / batch-create+batch-commit versions) stay 501 until the upstream
   Rust `DirectoryNamespace` (or a REST/managed backend) implements them — a parallel upstream contribution,
   independent of the merge. (Was "13"; version describe/create/delete + branches are now backed — see the
   COVERAGE correction.)
4. **Observability** — share rask's one GreptimeDB or keep separate per workload.


## Assessment 2026-07-15 (only §3 was still live)

*Was `docs/ASSESSMENT-2026-07-15.md`.*

## Assessment 2026-07-15 — catalog bench, rask-merge readiness, production readiness

> Historical report (2026-07-15). Some documents it cites — `KIND-RUNBOOK.md` among them — were
> retired on 2026-07-27 and live in git history; citations below are left as written.

Three parallel assessments (multi-agent, evidence-grounded) requested alongside the #42
model-registry UI ship. **Only §3 survives as live reference.**

!!! abstract "§1 and §2 were cut on 2026-07-28 (P8) — both discharged"

    **§1 (catalog bench)** was superseded by [`BENCH-2026-07-22.md`](BENCH-2026-07-22.md), which
    re-grounds the list against current code and applies the Lance-only scope decision.

    **§2 (rask-merge work-list)** described work that the merge then performed; it enumerated a
    pre-merge tree (`services/common`, the orchestrator, the `-api` services) that no longer exists.
    Residual items were carried into [`OPEN-WORK.md`](OPEN-WORK.md), which is the live backlog.

    Both sections remain in git history. **§3 below is intact** — it is the only in-tree
    gap-by-gap production-readiness enumeration, and `OPEN-WORK.md` C4 plus
    `runbooks/RUNBOOK-oncall.md` ("ASSESSMENT gap #5") both depend on it.

### 3. Production-readiness gaps

## Production-readiness assessment — lance-ns

Method: rendered `helm template lance-ns ./chart -f chart/values.yaml -f chart/values-prod.yaml` with the required prod overrides (image tags, `dapr.appToken`, `age.password`, `rustfs.secretKey`, `observability.edgeAuth.htpasswd`, ingress host) — the fail-closed render guards all work as documented — then inspected the full 5.7k-line manifest plus `chart/values*.yaml`, `chart/templates/*`, `docs/{DURABILITY,OPERATORS,DEPLOY,KIND-RUNBOOK}.md`. Render saved at `/tmp/claude-1000/-home-blackwell-Desktop-lance-ns/88508a85-af5d-44c1-9ef1-92d04ece7015/scratchpad/prod-render.yaml`.

What is already genuinely good: render fails closed on every placeholder prod secret; 0 plaintext secret *values* in env/args (only access-key IDs; secret keys are all `secretKeyRef` → `lance-ns-infra-credentials`); app tier fully hardened (runAsNonRoot, drop-ALL, seccomp, readOnlyRootFilesystem, /livez probes ×42, preStop drain); daprd sidecars resource-bounded; Dapr mTLS pinned on; audit stream ON in prod; `/produce` and `/demo` correctly gated off; PDBs + 2 replicas for the stateless tier; 14d observability TTL; pg-dump + VolumeSnapshot CronJobs exist; `age.externalHost`/`rustfs.externalEndpoint` externalization keys verified actually rewiring env (the values-prod header saying they "need follow-up hooks" is stale doc-drift).

### CRITICAL

1. **Dex is a demo IdP in the prod render** — `chart/templates/dex.yaml`: `storage: type: memory` (sessions lost on restart, hardcoded `replicas: 1`), static users alice/bob with bcrypt("password") hardcoded in the template, static client secret `lance-catalog-secret` in a plain ConfigMap, issuer `http://lance-ns-dex:5556/dex` (in-cluster HTTP — unreachable by real browsers), redirect `http://localhost:8080/callback`. `values-prod.yaml` doesn't touch dex at all, yet the entire governance layer (auth.enabled=true) rests on it. **Unnoticed** (no written deferral found). Fix: prod overlay must set an externally-reachable HTTPS issuer, a real storage backend (dex supports postgres — AGE is already there), connector to the org IdP (or at minimum remove the static demo users), and move the client secret into the infra-credentials/OpenBao path.

2. **No alerting engine at all** — confirmed: zero PrometheusRule/Alertmanager/rule-evaluator anywhere; the only trace is a Perses panel literally titled "alertable pair" (`perses-dashboards.yaml:148`). Nobody is paged when NATS dies, OpenBao seals, the outbox stops draining, or the DLQ fills. **Unnoticed.** Fix: GreptimeDB speaks PromQL (`/v1/prometheus`) — deploy vmalert (or Prometheus in rule-only mode) + Alertmanager with a real route; seed rules from the panels already labeled alertable (outbox depth/oldest-age, DLQ rate, error rate, NATS consumer lag, pod restarts).

3. **NetworkPolicy: zero objects in the prod render** — the full L3 layer (default-deny in/egress, DNS allow, exclusive client lists for openbao/age/rustfs) is *built* in `network-policy.yaml` but `networkPolicy.enabled` stays false and values-prod doesn't flip it. Compounding: the values file itself documents that lance-ray's in-cluster `/produce` ClusterIP route "stays reachable + unauthenticated" — with no L3 layer there is no compensating control against in-cluster cascade forgery. **Documented deferral** (kind CNI doesn't enforce; §7a runbook exists) — but the prod overlay is exactly where the flip belongs. Fix: `networkPolicy.enabled: true` in values-prod (+ `extraEgress` for any externalized backend), and note the policy-enforcing-CNI prerequisite there.

4. **Single-instance data/authz tier = platform-wide SPOF stack** — in the prod render: AGE Postgres 1 replica / 1Gi PVC (values-prod never sizes it) serving BOTH the lineage graph AND OpenFGA's datastore → AGE down = every governed request in the platform fails closed; NATS 1 node / `streamReplicas: 1` / 1Gi (bus down = no lineage ingest, no cascade — the parked **#20**); OpenFGA `replicaCount: 1` (stateless over PG — trivially scalable, not flipped); RustFS 1 replica on one 200Gi PVC = the entire lakehouse + observability store; OpenBao 1. **Documented deferral** ("stateful HA is the operators' job in rask — CNPG/rustfs-operator"; NATS externalize stanza commented in values-prod) — legitimate, but until the rask merge the self-contained prod path has no HA story. Fix now: `openfga.replicaCount: 2` (free), size `age.storage`, and treat the externalize stanzas (managed PG, clustered NATS `streamReplicas: 3`, managed S3) as the actual prod gate, not an optional footnote. Also: the auto-derived external DSNs keep `sslmode=disable` — externalizing PG today would go plaintext unless the openfga DSN is manually overridden.

5. **OpenBao sealed-on-restart = boot deadlock** — devMode=false is correct, but every pod restart/node drain leaves OpenBao sealed until a human runs `bao operator unseal`; apps consume secrets fail-closed via Dapr *at startup*, so app pods hang at "waiting for application startup" (the documented two-sided deadlock, `docs/OPERATORS.md` §5). No auto-unseal. **Documented deferral** (ESO / bank-vaults / vault-operator is "the destination"). Routine prod events (node drains, OOM) trigger it — do the operator adoption before calling this tier prod-ready, and alert on seal status (see gap 2).

### IMPORTANT

6. **Kind-only image assumptions baked into the chart** — app images render as bare local names (`lance-rest-catalog:v1.0.0`, no registry) and the chart has **no imagePullSecrets support anywhere** — unpullable on any real cluster except via node preloading. All images tag-pinned (good), none digest-pinned; one mutable third-party tag in the render (`ghcr.io/cloudoperators/greenhouse-extensions-integration-test:main`, openfga subchart helm-test pod). **Unnoticed.** Fix: registry-qualified `image.*.repository` in values-prod + `imagePullSecrets` plumbing in `_helpers.tpl`/pod specs; digests for the two first-party images.

7. **Resources: one-size-fits-all app tier, unbounded infra tier** — every app container gets `resources.default` (50m/128Mi → 1cpu/512Mi), including RustFS (the whole S3 data plane capped at 512Mi) and AGE Postgres (512Mi). Meanwhile GreptimeDB, NATS, Vector (a DaemonSet — unbounded on every node), OpenFGA, Perses, and the entire Dapr control plane have **no requests/limits at all**. **Unnoticed.** Fix: per-component resource keys for the first-party infra; pass resources through the subchart values (greptimedb-standalone, nats, vector, openfga, dapr).

8. **2-replica services have no spread/anti-affinity** — the only podAntiAffinity in the render belongs to the Dapr scheduler. catalog/lineage/gateway/web (replicas=2, PDB minAvailable=1) can co-schedule on one node → one node failure still takes the service to 0. **Unnoticed.** Fix: `topologySpreadConstraints` on `kubernetes.io/hostname` in the shared pod template.

9. **Dapr control plane left non-HA in prod** — values-prod doesn't set `dapr.global.ha.enabled=true`: operator/sentry/injector/placement all 1 replica. Placement is *hard-required by daprd 1.18 at boot* (your own values comment) — a placement restart during a rollout stalls every new sidecar; sentry down stops mTLS issuance. **Unnoticed** (one-line flip).

10. **Backups: destination shares fate with the primary, no retention, no restore runbook** — the pg-dump CronJob uploads lineage+openfga dumps to `s3://lance-catalog/_backups/pg/` — the *same RustFS PVC* the VolumeSnapshot protects, so a PVC/cluster loss takes primary and pg backups together; nothing prunes `_backups/` (unbounded growth inside the lakehouse bucket) or old VolumeSnapshots (the CronJob only creates); `snapshotClassName: ""` will fail on clusters without a default class; and there is **no restore procedure anywhere in docs** (grep "restore" in DURABILITY.md backup section = 0 hits). The externalize-to-operators posture is a **documented deferral**, but the retention/fate-sharing/restore gaps in the shipped mechanism are **unnoticed**. Fix: point pg dumps at an off-cluster bucket, prune both artifact kinds, set the class, write and *drill* a restore runbook.

11. **No TLS at the edge** — the rendered Ingress has no `tls:` block (values-prod leaves it empty, no cert-manager annotation): OIDC tokens, the edge-auth basic credentials, and vended S3 credentials would traverse plaintext HTTP. In-cluster, Dapr mTLS covers service-invocation/pubsub only; direct app→infra hops are plaintext (`sslmode=disable`, http RustFS/Greptime) — acceptable in-cluster, but the edge is not. **Unnoticed.** Fix: `ingress.tls` + cert-manager annotation in values-prod.

12. **Built-and-live-proven hardening switches silently omitted from values-prod** — `security.serviceAccounts` (every app pod runs as SA `default` with token automount on), `security.infraContexts`, `dapr.sidecarRestricted` all false in the prod render, despite memory recording them "left ON and proven" on the 2026-07-13 live pass. PSA `restricted` enforce is separately **parked** (OTel Collector hostPath — documented, KIND-RUNBOOK §6.4). The values-prod omission of the proven switches looks like an oversight, not a decision. Fix: flip all three in values-prod (with the documented `dapr_sidecar_injector.sidecarDropALLCapabilities=true` companion).

13. **`deployment.environment.name=kind` ships in the prod render** — values-prod never overrides `observability.environment`; every OTel resource attr will claim prod telemetry is kind. **Unnoticed.** One line.

14. **Manual out-of-band install-order footguns** — values-prod itself warns: flip `medallion.fgaEnabled` without first running `scripts/seed_medallion_fga.sh` and the movers fail closed / pipeline stalls; OpenBao needs manual init+unseal; the namespace PSA label is a manual kubectl step. There is no prod install runbook — DEPLOY.md is kind-framed ("how it all works on kind"), OPERATORS.md is strategy. Fix: a PROD-RUNBOOK.md (ordered: secrets → seed FGA → unseal → flip governance → verify), plus consider a seed Job/hook for the FGA grants.

### NICE

15. `moverReplicas: 1` throughput/availability ceiling — **documented deferral** (process-local single-flight lock; raise after a cross-pod lock ships). Fine as-is; NATS redelivery + idempotence bound the damage.
16. nats-box debug shell Deployment ships in the prod render — set the nats subchart's `natsBox.enabled=false` in values-prod.
17. Dapr scheduler STS: 3×16Gi PVCs for actors/workflows/jobs the stack doesn't use — shrink the PVC size via subchart values.
18. Retention odds: `runRetentionDays: 0` (Run nodes grow forever — prod has the reconcile pruner deployed but the knob off), `compaction.lineageEmit: false` (the compaction FAILURE lineage surface stays dark in prod), `freshnessBudgetHours: 0`. Audit-stream retention sharing the 14d observability TTL is a **documented deferral** — note 14d is short for a compliance trail.
19. HPA off (documented optional, needs metrics-server) — fine; enable once resource requests are truthful (gap 7), since HPA math depends on them.
20. values-prod header's claim that the externalize stanzas "do nothing without follow-up hooks" is stale — verified `age.externalHost` rewires lineage DSN, openfga DSN, wait-init and pg_dump correctly. Fix the comment so operators trust the mechanism.

### Deferred vs unnoticed — the roll-up
- **Written deferrals (don't re-litigate, schedule):** NATS HA/externalization (#20 parked), stateful HA via rask operators (CNPG/rustfs-operator), OpenBao auto-unseal via ESO/bank-vaults (OPERATORS.md §5), audit retention = observability TTL, PSA restricted enforce (OTel Collector hostPath), L3 default-deny known un-flipped (§7a runbook), moverReplicas=1, mode_b vending.
- **Genuinely unnoticed:** Dex demo-IdP posture in prod, no alerting engine, backup fate-sharing/retention/restore, no registry/imagePullSecrets plumbing, unbounded infra resources, no anti-affinity, dapr.global.ha unflipped, values-prod omitting the live-proven SA/infraContexts/sidecarRestricted switches, environment=kind attr, no edge TLS, no prod install runbook.


## Catalog feature bench 2026-07-22 (Polaris / Unity / Lakekeeper)

*Was `docs/BENCH-2026-07-22.md`.*

## Catalog feature bench — currency refresh (2026-07-22)

**Question re-asked (2026-07-22):** are we missing an *essential* catalog feature that
**Polaris**, **Unity Catalog**, or **Lakekeeper** have?

**Method:** re-grounded the standing bench (`ASSESSMENT-2026-07-15.md` §1, itself built on the
since-retired `FEATURE-GAP.md` — its recorded deviations now live in `DECISIONS.md`) against the *current* code — the 16 wired catalog routers
(`services/catalog/api/v1/router.py`), `packages/service-kit/src/service_kit/governed/fga.py`, and the git log since 2026-07-15.
This is a **currency refresh of OUR side**: it re-checks which of the 2026-07-15 "genuinely missing"
items we shipped in the intervening week, and what genuinely remains. Competitor states are the
snapshot the 2026-07-15 bench currency-checked (Polaris 1.5.0, Unity Catalog OSS 0.4.x–0.5.0,
Gravitino 1.2–1.3, Nessie, Lakekeeper 0.13.1) — this refresh does not re-survey their releases.

> **Scope decision (2026-07-22): Lance-only.** The product intent is to operate a **Lance**
> lakehouse, not to project into or interop with other catalogs. This retires the two federation-shaped
> items below (§ "Genuinely missing" #1 and the generic-table/Delta-Sharing tail) as **deliberate
> non-goals**, not gaps — they exist only to make Lance tables visible *inside* Iceberg/Polaris/UC/
> Lakekeeper, which is explicitly out of scope. With that scope applied, **no in-scope catalog feature
> is missing.** The remaining in-scope items are §2 control-plane change-events (Lance-internal
> consumers) and the SaaS-scale / native-Lance-blocked tail.

---

### TL;DR

**No table-stakes hole remains.** Vending, physical multi-tenancy, refs (branch/tag/time-travel),
soft-delete/undrop, discovery search, model registry, **maintenance-policy store**, **access review**,
writable business metadata, and console breadth are all present, and lineage still exceeds all three
competitors. The 2026-07-15 bench listed five "governance-plane convenience" gaps the 2026 crop
converged on; **four of the five shipped this week**. What's left is one genuinely-valuable gap
(**catalog federation**), one lower-urgency gap (**control-plane change-events**), and a short tail of
SaaS-scale / native-Lance-blocked / by-design items.

---

### Closed since 2026-07-15 (verified in code)

The 2026-07-15 "genuinely missing and valuable" table had eight rows. Current state:

| 2026-07-15 gap | Status today | Evidence |
|---|---|---|
| Business-metadata / **tag management API** | ✅ shipped (#49) | governed tag/description writes on the lineage service |
| Declarative **table-maintenance policy store** | ✅ shipped (#50) | `api/v1/endpoints/policies.py` — owner-gated `policy/set|delete|describe`, sweep enforces off the bucket, tag-pinned versions exempt |
| **Access-review** ("who can access X") | ✅ shipped (#51) | `api/v1/endpoints/access.py` → `fga.list_users` (`common/fga.py:395`); effective access across role/team/parent cascade, owner-gated, fail-closed, `access_review` audit event. *This was the "FGA `list_users` never called" gap — now called.* |
| **Admin / console UI scope** | ✅ broadened (#52 + Phase A #72–#83) | grant/revoke (#72), index build/rebuild/drop (#73), FGA relationship-graph explorer with inline grant (#81), schema evolution + table/column properties (#74), on-demand GC preview/run (#75), compaction target-size + compact-now (#76), audit-log viewer (#77), format badge + prop rejection (#78), quality-gate badge (#82), DLQ ops panel (#83) |
| Multi-storage-profile + **SSE-KMS / remote-signing** vending | ⏳ still open (SaaS-scale) | one profile per warehouse; `core/vending.py` = ModeB/Static/STS/WebIdentity, no KMS/signing |
| **Outbound catalog federation / generic-table registration** | ❌ **still missing** | `grep -ri federat services/` → **0 hits** (only the assessment doc). lance-ns is still an island. |
| Catalog **change-events / webhook framework** | ⏳ partial (data-only) | `core/lineage_emit.py` publishes *data* events (OpenLineage over Dapr/NATS); no control-plane event stream (grant-changed, warehouse-lifecycle, policy-set) — `grep` for warehouse/grant events → none |
| Catalog-level branching/rollback (Nessie whole-catalog) | ⏸ parked by design | per-table refs + atomic `batch-commit` cover the realistic Lance workflows |

---

### Genuinely missing today — ranked

#### 1. ~~Outbound catalog federation / generic-table registration~~ — **out of scope (Lance-only)**
- **Decision (2026-07-22):** deliberate non-goal. This item exists only to make Lance tables visible
  *inside* Iceberg/Polaris/UC/Lakekeeper (Polaris 1.5 federation, Lakekeeper 0.13 Lance pointers,
  Gravitino's lance-namespace dialect, UC generic pointers). The product supports **Lance only**, so
  projecting into foreign catalogs is not wanted. Kept in the record for completeness, not as a backlog
  item. (Confirmed unbuilt either way: `federat` appears in no source file.)

#### 2. Control-plane change-events / webhook framework — **the top in-scope item**
- **Who:** Polaris 1.5 (multiple event listeners, per-event-type opt-in); Lakekeeper (CloudEvents to
  NATS/Kafka).
- **Us:** we emit **data** events (OpenLineage over Dapr/NATS on table lifecycle + writes, incl. drop)
  and **audit** events (OTLP). We do **not** emit **control-plane** events — grant/tuple changed,
  warehouse activated/deactivated, namespace created, policy set — as a generic subscribable stream.
- **Why it matters:** Lance-internal consumers (cache invalidation, a UI live-refresh, an in-estate
  sync/reaction worker) need "a grant changed" / "a warehouse was deactivated" without tailing the audit
  log. Still lower urgency because OpenLineage-over-NATS already *is* our event bus for data changes; the
  gap is the non-data control plane. The transport (Dapr pub/sub over NATS) already exists — this is a
  new topic + emit calls at the mutation sites, not new infrastructure.

#### 3. SSE-KMS / remote-signing credential vending — compliance-tenant feature
- **Who:** Lakekeeper (SSE-KMS vending, Iceberg remote signing); UC (external locations + storage
  credentials as first-class objects).
- **Us:** `core/vending.py` covers ModeB / Static / STS / **WebIdentity** with per-table session
  policies — **on par** for the common case, but no KMS-encrypted vending and no remote-signing path.
- **Why it matters:** only for compliance-heavy tenants that mandate KMS envelope encryption or signed
  requests. Already marked "on par, SaaS-scale only" — still true.

---

### The tail (valuable but narrower / blocked / SaaS-scale)

- **Views / materialized views** — endpoints exist (`views.py`) and seed FGA, but the native backend
  still maps `create_materialized_view` to **501** (`services/catalog/services/native.py` →
  `UnsupportedOperationError`). Blocked on native Lance view-deps (`base_objects`), **not ours to fill**.
  The medallion gold layer is the MV equivalent today.
- **Multi-storage-profile per warehouse / external-locations-as-first-class** (UC) — one profile per
  warehouse (#3-A/#35). SaaS-scale only.
- **Cross-org open-share protocol** (UC Delta Sharing — recipient-token cross-org table sharing) —
  none, and **out of scope by the Lance-only decision** (it is a foreign-protocol interop surface).
  Our governed blob Range proxy + credential vending is the same-org analog we keep.

---

### N/A-by-design (do **not** build — unchanged from 2026-07-15)

Iceberg REST facade / Iceberg commit coordination (we operate Lance, not Iceberg pointers);
functions/UDF management (no SQL engine); UC volumes as ungoverned file dirs (blob-v2 in-table is the
deliberate alternative); OPA/Ranger/Cedar external-authorizer pluggability (OpenFGA is committed,
non-substitutable per the k8s/Dapr audit); geo-distributed metalake (wrong scale); inbound federation
of Hive/JDBC/ClickHouse catalogs (we are a lakehouse catalog, not a metadata lake); presigned URLs
(governed proxy chosen deliberately — signed URLs bypass ReBAC); multiple OIDC providers (Dex
federates IdPs); metric views / semantic layer (blocked on native Lance MV); UC VARIANT type
(Arrow-native types cover it).

---

### Bottom line

After this week's shipping **and the Lance-only scope decision**, **no in-scope catalog feature is
missing.** The two genuinely-valuable items that remained on 2026-07-15 were both foreign-catalog
interop (federation, generic-table/Delta-Sharing) — now **deliberate non-goals**, since the product
operates a Lance lakehouse and does not project into Iceberg/Polaris/UC/Lakekeeper. The only in-scope
build left is **control-plane change-events** for Lance-internal consumers (#2, low urgency; the NATS
transport already exists); the rest is SaaS-scale (KMS vending, multi-profile) or blocked on native
Lance (views/MVs). Lineage remains the moat none of Polaris/Unity/Lakekeeper match.


## Design — interactive state

*Was `docs/DESIGN-interactive-state.md`.*

## Where interactive state belongs — and why `setInterval` is the symptom, not the disease

Owner's question, asked three times and answered thinly each time until now: *"how come KV, cache, state
management or actors — any of it from Dapr — is not being used for this stuff? Lance is about OLAP and
storage, so how come."*

The short answer: **they are right.** The analytical plane is coherent; the *interactive* plane has no home
for its state, and the frontend's 15 polling timers are what that absence looks like from the outside.

### The causal chain, measured

| Layer | What we found | Evidence |
| ----- | ------------- | -------- |
| Frontend | 15 files poll with `setInterval`; `query.live` used in exactly **one** file; **zero** `EventSource`; no client query cache | `grep -rl setInterval` per zone; `admin.remote.ts` |
| Media-plane services | viewer / search / annotator: `publish:0 subscribe:0`, zero Dapr imports — sidecar and tracing only | vs medallion 4/7, catalog 2/2, lineage 0/3, compaction 1/2 |
| Dapr building blocks in use | pub/sub only | no state store, no actors, no workflow component in `chart/` |
| Store for operational state | none — so there is nowhere to put a task's state except Lance, which is the wrong shape | — |

Read it downwards and it is one fault, not four: **the UI polls because there is no event to subscribe to;
there is no event because those services publish nothing; they publish nothing because there is no
operational state model to publish *about*.**

### Why Lance must not hold this state

Lance is a columnar, immutable, **versioned** analytical format. Every commit is a new version with a
manifest and a transaction file — which is exactly why the git-like history in `#113` works so well.

That same property makes it wrong for interactive state. A per-task flip (`assigned → in progress →
submitted → reviewed`) is a small, frequent, single-entity write. In Lance each one would be a dataset
version: hundreds of manifests a minute, a version history that is noise rather than provenance, and
read-modify-write contention between annotators on a format with no row-level locking. It is not a
limitation of Lance; it is a category error to ask an OLAP format for OLTP semantics.

### Which store, decided — two, each chosen for a property it has (owner-approved 2026-07-26)

The owner's constraints: *"I'm fine with adding redis as well for cache etc. As long as cloud native and
makes sense together. However still want to use jetstream for event driven workflows of course, due to its
complex and high performance."* Both are satisfied, and the split is not arbitrary.

| Purpose | Component | Why this one and not the other |
| ------- | --------- | ------------------------------ |
| Expensive shared reads, hot ephemeral state | **`state.redis`** with `ttlInSeconds`, `actorStateStore: false` | A cache must *forget*. TTL eviction and memory-bound behaviour are what Redis is actually best at, and `ttlInSeconds` is a first-class metadata field |
| Actors, workflow, durable domain state (annotation tasks, the notification inbox) | **`state.postgresql`** on the already-deployed `lance-ns-age-0`, `actorStateStore: "true"` | Durable workflow that loses its state on a pod restart is not durable. Postgres is durable by default and already backed up and monitored; a Redis without AOF configured in kind loses everything on restart. Stable actor support in v1 and v2 |
| The event backbone | **`pubsub.jetstream`** — unchanged | Already deployed and already correct, including the broadcast variant. No change at all |

Two state stores is idiomatic Dapr, not a compromise: components are named by purpose, and a service asks
for the one whose guarantees it needs. What would be wrong is one store pretending to be both — a cache
that must not lose data, or a durable store that must evict.

**On the Redis image:** Dapr's Redis state store is tested against **Valkey 8.x and 9.x**, so Valkey works
and is BSD-licensed rather than Redis Ltd's RSALv2/SSPL. The one caveat from the component reference: stock
Valkey images bundle neither RediSearch nor RedisJSON, so the **Query API** and the `queryIndexes` metadata
field will not work. Neither is needed here — a cache and a KV inbox use get/set/TTL, transactions and
ETag — so record the constraint and do not reach for the Query API later expecting it to be there.

**JetStream stays, and Dapr Workflow does not replace it.** These are different tools and it matters:
the medallion cascade (`medallion.bronze` → `medallion.silver` → …), control events and lineage delivery are
**event-driven fan-out**, which is exactly what JetStream is for and is already high-performance and proven
here. Dapr Workflow is **orchestration with queryable status** — a named instance, a step counter, retries
and compensation. It is additive, for the `#122` publish saga and export jobs where a user needs "step 3 of
7" and a failed step must roll back. Using workflow to replace the cascade would be a regression; using
pub/sub to report progress is what forced 15 polling timers.

### The shape that fits, per building block

- **Lance** — published, immutable, versioned analytical data. Unchanged, and correct as it is.
- **Dapr state store (KV)** — split as decided above: annotation project/task state, assignments, review
  states and feed cursors on Postgres; caches of expensive shared reads on Redis with a TTL. Small frequent
  reads and writes, no version per keystroke.
- **Dapr actors** — one actor per task (or per project): single-threaded per entity, so two annotators
  cannot claim the same task, and a progress counter needs no lock. Actor **reminders** give claim leases
  for free — a task claimed and abandoned returns to the queue without a sweeper cron.
- **Dapr workflow** — the *sync* in "synced only when we choose to" (`#122`) is a saga: freeze the project,
  write the governed table, emit lineage, tag the version, mark published. Durable, retried, resumable.
  Note the skill's constraint: workflow uses the actor framework internally, so it needs that state store
  with `actorStateStore: "true"` — the state store is a prerequisite, not an alternative.
- **Dapr pub/sub** — the change signal. It already exists in the medallion plane and is absent in the media
  plane, which is why the annotator can save a label and nothing downstream reacts.
- **SvelteKit `query.live`** — the UI's subscription to that signal. The docs are explicit: live queries
  "do not have a `refresh()` method, **as they are self-updating**". Mutations use `command`/`form`, which
  invalidate dependent queries and return the refreshed data **in the same round trip** (single-flight), so
  after your own write you wait for nothing.

### What this replaces

Every `setInterval` in the frontend, and the idea that annotation state could live in the governed plane.
Neither is a small cleanup: the timers are a workaround for a missing subscription, and the state question
decides whether `#122` is buildable at all.

### Three corrections this document needed (design fan-out, 2026-07-26)

Written before a measured design pass, the plan below had three faults. All three are corrected here, and
the corrections are cheaper than what they replace.

**1. Do not put Dapr sidecars on the four zone pods.** This document implied the zones needed to reach a
state store themselves. They do not, and a sidecar there is the most expensive item on the list: +127 MiB
measured (4 × ~31.7 MiB working set — 1.4× the annotator zone's own memory); the zone base path makes the
Dapr app channel unreachable, so `/dapr/subscribe` is a 404 on `web-media` and programmatic pub/sub would
fail *silently*; `annotator` is already a Dapr app-id (`chart/values.yaml:789`), a direct collision; and the
zones are readiness-probed by a TCP dial while every backend probes its own sidecar's health, so a zone with
a sidecar would serve traffic before Dapr enrolled, on every rollout. Meanwhile `catalog`, `lineage`,
`viewer`, `search` and `annotator` are **already 2/2**. Anything needing a store, an actor or a workflow
lives in one of those, and the zone BFF reaches it by HTTP proxy — which is its entire job already.

**2. The cache belongs in the browser, not in the BFF.** This is the correction that dissolves the tenancy
risk. A BFF cache of authorized reads needs a key derived from the subject, and getting that key wrong is a
cross-user leak rather than a slow page. An in-app memo needs no such key: it lives in the user's own tab, so
it is per-user by construction, immune to the `replicas: 2` coherence problem (there is no session affinity
on the Ingress), and it has no failure mode when a component is down. The biggest measured waste in the
estate is `/media/api/atlas/points` at **6,679,228 bytes on every mount and every Text/Visual toggle** —
25.5 MiB in ~30 s of ordinary clicking, which OOM-killed the viewer mid-measurement and took the media plane
to 502. That is `#121` with a cause attached, and the fix is a memo keyed on the `v=6` token already in the
URL. Version tokens and content hashes make invalidation **free**: a new build changes the key and the old
entry is unreachable.

**3. A per-user change feed already exists, and it is not the catalog's.** The claim that `query.live` was
blocked on event scoping is true of the **catalog** control feed only (`can_observe_events`, estate admin).
The **lineage** service's `GET /events` is already per-subject governed — an event is shown only if the
caller `can_get_metadata` on *every* dataset it references — and already has a keyset cursor (`after`). The
TypeScript client implements it (`packages/api/src/lineage/client.ts:117-126`) and **no caller passes it**.
The lineage plane is where 8 of the 13 lakehouse pollers live, so a non-admin's live refresh is available
today with no backend change and no Dapr: `admin.remote.ts` pointed at lineage instead of the catalog.

### Order of work

**Step 0, prerequisites and free wins — before any `query.live` expansion.**

- `nginx.ingress.kubernetes.io/proxy-read-timeout` on the ingress. Confirmed live: the running controller
  has `proxy_read_timeout 60s`, there is no override annotation, and SvelteKit's SSE transport emits **no
  keepalive** (kit 2.70.1, `runtime/server/remote.js:90` is the only `enqueue`, no timer anywhere in
  `runtime/server`). So an idle live feed is severed every 60 s, and each reconnect re-primes the whole
  200-event window and writes an audit record. Replicating `query.live` 15× without this would make the
  estate **slower while looking faster**.
- Pass `summary: true` on the lineage jobs page — measured **464,318 → 46,980 bytes**, 10×, on a page
  currently moving 528 MB/hour to render one job. The flag is already passed one file over.
- One health fetcher per zone in media. **Done** — `7f688d6`, with the invariant now enforced by a test.

**Then, in impact order.**

1. **The browser memo** for the atlas projection, content-addressed thumbnails and the descriptor. Highest
   user-visible gain per unit of work in the whole inventory, and the fix for `#121`. No infra, no chart, no
   backend change.
2. **Adopt what SvelteKit already ships**: data `load` functions (deployed, used by exactly one page), and
   `query` in place of the 13 hand-rolled poll/loading/401/offline triples — whose four-way drift is
   currently a correctness bug, since two lineage pages keep rendering governed rows after the session dies.
3. **One `query.live` per feed on the lineage cursor** (correction 3). Deletes the remaining timers and
   fixes the opposite failure at the same time: four admin surfaces make **zero requests, ever**.
4. **`form` + single-flight + `withOverride`** at the six mutation sites, removing the trailing `await
   load()` round trip after every write.
5. **The two state store components** (Redis for cache, Postgres with `actorStateStore: "true"` for actors
   and workflow), then `#122`'s task state on Postgres, then publish-on-save so there is an event rather
   than a poll, then workflow for the publish saga and `#125`'s notification inbox.

Note the reordering: the store moved from first to last. Steps 0–4 need no new component at all, and they
carry most of the measured user-visible gain. The store is required for `#122` and `#125` — durable task
state and a notification inbox genuinely cannot be built without it — but it was never the prerequisite for
making the existing surfaces feel responsive, which is what the polling timers were about.


## Design — annotation projects

*Was `docs/DESIGN-annotation-projects.md`.*

## Annotation projects — the task domain, its own state, and what publishing emits

Owner's design for `#122`, stated twice and corrected once:

> "Annotate should be more like annotate-project and not the gallery. We don't pick individual like that.
> More that we select from search or elsewhere and send to annotate."

> "annotate should not have the state of annotation… only when we choose to sync. So labeling or sending to
> annotate is a different project management than appshell. With tasks of what should be done similar to any
> other labeling platform."

So the annotator is **its own project/task domain with its own state**, not a view over the lakehouse. Items
arrive by being *sent* from search / atlas / a saved view. The landing page is your projects and their
progress. A finished project is **published** to the lakehouse as a governed table plus lineage, and nothing
lands before that.

An earlier note of ours said a project should "reference governed table rows". That is wrong and is the exact
coupling the owner ruled out. It is not reintroduced here: see [Items are captures, not
references](#45-items-are-captures-not-references).

This document decides the schema. `docs/DESIGN-interactive-state.md` argued *where* the state belongs; this
one says *what* the state is.

### Status — what is decided, what is built

**Decided and durable:** everything in §2–§10. The entities, the two state machines with their transition
tables, the authz doors, the publish contract, and the slice plan.

**Built: nothing yet.** No code landed with this document, and that is deliberate rather than unfinished:

* §8 slices `S1`–`S4` are implementable today — no state store, no chart change, real tests. They land in
  `services/annotator/projects/`, `packages/service-kit/src/service_kit/governed/auth/`, `services/catalog/api/v1/endpoints/data.py` and
  `tests/unit/`. The run that produced this document was scoped to `docs/**` (plus a new service under
  `services/annotations/**`, which §3 refuses), so none of those four could be written here. They are
  specified down to the file, the test and the failure message, so the next run is transcription, not design.
* §8 slices `S5`–`S10` are blocked on `#124` for a reason no amount of effort routes around: **there is no
  store in the cluster to put a project in** (§1.2), and shipping the HTTP surface or the landing page on a
  volatile store would manufacture exactly the false "done" the owner has rejected before.

The one thing this document must not become is code written to look busier than it is. §1.1 earns its keep on
its own: the measurement kills the design we would otherwise have shipped.

---

### 1. What exists today, measured

`services/annotator` is a FastAPI service in the media plane. Its annotation state is entirely in a Lance
table, written directly on every save:

| Piece | Where | File |
| ----- | ----- | ---- |
| Per-shape rows (`id`, geometry, `label`, `status`, `reviewer`, `confidence`…) | Lance table `annotations` | `services/annotator/annotations/schema.py` (`EMPTY_SCHEMA`) |
| Save = `merge_insert("id")` → one new Lance version | Lance | `annotations/save.py` |
| Optimistic concurrency = the client's `base_version` vs the Lance table version | Lance | `annotations/commit.py:check_base_version_value` |
| "Who / when" audit trail = the `reviewer` column + Lance version timestamps | Lance | `annotations/versions.py` |
| Batch chunk tags = annotation rows across many units, one version | Lance | `annotations/tags.py` |
| Landing page = a **gallery**: datasets → documents → chunks | zone | `frontend/.../annotator/src/lib/select/DataSelection.svelte` |
| Selection = ephemeral `?keys=doc/speech/chunk` in the URL, no project | zone | `src/lib/labeling/review-selection.svelte.ts` |

There is no project, no task, no assignment, no review, no publish step. `status` on a row
(`accepted` / `rejected` / `prediction` / `reviewed` — `src/lib/viewer/layout/statusStyle.ts`) is the only
lifecycle concept, and it lives in the governed table.

#### 1.1 The measurement that decides the store

The deployed annotations table, on the live kind cluster (`/media-corpus/transcripts_v2.lance/`):

```
$ kubectl exec deploy/lance-ns-annotator -c annotator -- python -c '…ds.count_rows(), ds.version, len(ds.versions())'
annotations  rows=      3 version=  615 n_versions=615
chunks       rows= 145175 version=   24 n_versions=24
documents    rows=   1154 version=    1 n_versions=1
```

**Three rows. 615 versions.** The analytical tables next to it — 145,175 chunks — have 24 and 1. On disk:

```
$ kubectl exec … -- sh -c 'cd …/annotations.lance && du -sh _versions data _transactions && ls … | wc -l'
2.5M   _versions        (616 manifests)
4.9M   data             (581 data files)
2.5M   _transactions    (615 transaction files)
→ 9.8M total for 3 rows
```

Both blocks re-measured on 2026-07-26 and byte-identical to the first reading: the table is idle, so this is
the standing cost of four days of one developer clicking, not a live-churn artefact.

Peak churn was 15 versions per minute (`collections.Counter` over `ds.versions()` timestamps, busiest
minutes: `2026-07-21 09:57`, `11:40`, `20:02`, `20:48`, `2026-07-22 12:10` — 15 each).

That is correlation. Here is the causal probe — run in the annotator pod against the exact production
runtime (`pylance 8.0.0`, CPython 3.13.14), doing nothing but what `save.py` does: a single-field flip on a
three-row table via `merge_insert("id")`, twenty times.

```
after seed:                  version 1  data files 1
after 20 single-field flips: version 21  n_versions 21  rows 3
                             data files 21  manifests 22  txn files 21
                             bytes: data 13419  manifests 10383  txns 2858
                             per flip: manifest 472 B  txn 136 B
```

**One state flip = one dataset version + one new data file + ~470 bytes of manifest + ~136 bytes of
transaction.** Twenty flips on three rows produced 26 KB of files and 21 versions. Row count never moved.

To re-run it (writes and removes `/tmp/probe.lance` inside the pod, touches nothing governed):

```sh
kubectl exec deploy/lance-ns-annotator -c annotator -- python -c '
import shutil, os, lance, pyarrow as pa
p="/tmp/probe.lance"; shutil.rmtree(p, ignore_errors=True)
schema = pa.schema([("id", pa.string()), ("state", pa.string())])
ds = lance.write_dataset(pa.table({"id":["a","b","c"], "state":["unassigned"]*3}, schema=schema), p, schema=schema)
du = lambda s: (sum(os.path.getsize(os.path.join(r,f)) for r,_,fs in os.walk(os.path.join(p,s)) for f in fs),
                sum(len(fs) for _,_,fs in os.walk(os.path.join(p,s))))
print("after seed: version", ds.version, "data files", du("data")[1])
for i in range(20):
    row = pa.table({"id":["b"], "state":[f"claimed-{i}"]}, schema=schema)
    lance.dataset(p).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(row)
ds = lance.dataset(p)
print("after 20 flips: version", ds.version, "n_versions", len(ds.versions()), "rows", ds.count_rows())
print("data", du("data"), "manifests", du("_versions"), "txns", du("_transactions"))
shutil.rmtree(p, ignore_errors=True)'
```

Scale it at the owner's own product shape — a ten-person team, one task decision per person per minute:
**600 dataset versions per hour**, ~0.35 MB/hour of manifests and transaction files alone (600 × 608 B)
before a single label byte, one new data file per decision, and a version history in which the provenance of
the *data* is buried under the churn of *workflow state*. The bytes are not the argument; the 600 versions
are. The 615-versions-for-3-rows table is what that looks like after four days of one developer clicking.

This is not a Lance defect. Lance is an immutable, versioned, columnar analytical format; a version per
commit is precisely why the git-like history in `#113` works. Asking it to absorb a per-task status flip is a
category error. **Confirmed: task state cannot live in Lance.**

#### 1.2 What the cluster has to hold it instead — nothing, yet

```
$ kubectl get components.dapr.io -o custom-columns=NAME:.metadata.name,TYPE:.spec.type
catalog-control-pubsub            pubsub.jetstream
compaction-cron                   bindings.cron
lance-secrets                     secretstores.hashicorp.vault
lineage-pubsub…(7)                pubsub.jetstream
```

Pub/sub, a cron binding, a secret store. **No state store, no actor state store, no workflow.** That is
`#124`, and §8 draws the fence: four slices are on this side of it, six are on the other.

---

### 2. References — how real labeling platforms model this

Cited from source and docs, not invented.

#### Label Studio (HumanSignal)

* **Entities**: `Project` → `Task` → `Annotation` (+ `Prediction`, `AnnotationDraft`, `TaskLock`).
  [`label_studio/tasks/models.py`](https://github.com/HumanSignal/label-studio/blob/develop/label_studio/tasks/models.py)
* `Task` carries **denormalized progress**: `is_labeled`, `overlap` ("number of distinct annotators that
  processed the current task"), `total_annotations`, `cancelled_annotations`, `total_predictions`,
  `precomputed_agreement`, `comment_count`, `unresolved_comment_count`.
* `Annotation` carries `completed_by`, `was_cancelled` (help text: *"User skipped the task"*),
  `ground_truth`, `lead_time` (*"Time in seconds to label the task"*), `draft_created_at`, `result_count`,
  `parent_prediction`, `parent_annotation`, `last_action`.
* `Annotation.last_action` ∈ `ActionType`
  ([`tasks/choices.py`](https://github.com/HumanSignal/label-studio/blob/develop/label_studio/tasks/choices.py)):
  `prediction`, `propagated_annotation`, `imported`, `submitted`, `updated`, `skipped`, `accepted`,
  `rejected`, `fixed_and_accepted`, `deleted_review`.
* **`TaskLock(task, user, expire_at)`** — a claim *lease*, not an assignment column.
* `Project.skip_queue` ∈ `REQUEUE_FOR_ME` / `REQUEUE_FOR_OTHERS` (default) / `IGNORE_SKIPPED`, with the
  source comments: *"requeue skipped tasks back to the common queue, excluding skipping annotator"* /
  *"ignore skipped tasks => skip is a valid annotation, task is completed"*.
  [`projects/models.py`](https://github.com/HumanSignal/label-studio/blob/develop/label_studio/projects/models.py)
* **Explicit state machine, append-only.** `label_studio/fsm/state_choices.py`:
  `TaskStateChoices = CREATED | IN_PROGRESS | COMPLETED`; `ProjectStateChoices = CREATED | IN_PROGRESS |
  COMPLETED`; `AnnotationStateChoices = CREATED` (*"Annotations don't carry state in LSO"*). The state rows
  are **insert-only** — `label_studio/fsm/state_models.py` says *"No constraints needed - INSERT-only approach"* and
  current state is "determined by latest UUID7 id".
* **Reviewer actions** ([review guide](https://docs.humansignal.com/guide/quality)): **Accept**,
  **Fix & Accept**, **Reject** — and, verbatim, *"Rejecting an annotation does not return it to annotators to
  re-label."* To re-label you must delete the annotation.
* Assignment is Enterprise-only: *"You can't assign annotators to specific tasks in Label Studio Community
  Edition."*

#### CVAT

* **Entities**: `Project` → `Task` → `Segment` → `Job`. The **job** is the unit of assignment (a slice of a
  task's frames). [`cvat/apps/engine/models.py`](https://github.com/cvat-ai/cvat/blob/develop/cvat/apps/engine/models.py)
* **Two axes** on a job:
  `StageChoice = annotation | validation | acceptance` and
  `StateChoice = new | "in progress" | completed | rejected`.
  The source carries its own migration TODO: *"it has to be deleted in Job, Task, Project and replaced by
  (stage, state)… The stage field cannot be changed by an assignee, but state field can be."*
* `AssignableModel` = `assignee` + `assignee_updated_date`. Managers assign: *"assign jobs to annotators by
  adding the annotator name to Assignee and changing the Job stage to Annotation"*;
  validators likewise at stage Validation.
  [workflow guide](https://docs.cvat.ai/docs/guides/workflow-org/)
* **Review = issues, and rejection returns work.** `Issue(frame, position, job, owner, resolved)` +
  `Comment(issue, owner, message)`. On rejection you *"reassign jobs to either the Validator or Annotator"* —
  the opposite of Label Studio's dead end.
* `JobType = annotation | ground_truth | consensus_replica`. **Consensus** is a separate subsystem: odd
  numbers of replicas, a manager-run merge with majority voting, and *"Merging overrides annotations in the
  parent job. This operation cannot be undone."*
  [consensus](https://docs.cvat.ai/docs/qa-analytics/consensus/)

#### What we take, and what we refuse

| Reference behaviour | Our decision |
| ------------------- | ------------ |
| LS `TaskLock(user, expire_at)` — claim as a lease | **Take.** A lease is the only claim model that is correct under crash. |
| CVAT `stage` × `state` — two axes | **Refuse.** 3 × 4 admits meaningless combinations (`acceptance`/`new`), and CVAT's own source is mid-migration away from a third overlapping `status` field. One axis, named transitions. |
| LS reviewer vocabulary Accept / Fix & Accept / Reject | **Take the vocabulary.** |
| LS "rejecting does not return it to annotators" | **Refuse.** Named `request_changes`, and it returns the task to a claimable state — CVAT's reassign-on-reject behaviour. A review that cannot ask for a fix is not a review. |
| CVAT `Issue(frame, position)` — positioned review issues | **Flatten.** Review notes are an append-only list on the task with optional `shape_ids`, no canvas geometry. Named gap, not a hidden one. |
| LS `overlap` / `maximum_annotations` / `precomputed_agreement`, CVAT consensus replicas + merge | **Out of v1, explicitly.** One annotation per task. Consensus needs N independent annotations, an agreement metric and an irreversible merge algorithm; we have none of the three and no user asking. |
| LS denormalized counters on the task/project | **Take.** The landing page is "projects and their progress"; the counters are that page. |
| LS insert-only FSM state rows | **Take.** The transition log is append-only and is the audit trail. |
| CVAT manager-assigns; LS Community cannot assign at all | **Both, unified.** Self-serve claim with a lease; a manager `assign` is a claim on someone else's behalf with no expiry. |

---

### 3. Decision: where the domain lives

**One service, one new resource package: `services/annotator/projects/`.** No new service.

* `services/annotator` is already deployed with a Dapr sidecar (`lance-ns-annotator-… 2/2 Running`), already
  behind the estate's OIDC/FGA door, and already laid out as one package per resource (`annotations/` is one
  today). A second service buys a chart object, a dockerfile, a CI image (`#118`: CI does not build these
  yet) and a second door — for zero domain benefit.
* The two halves are one product surface: the projects landing page and the annotate canvas are the same
  zone.
* The project package needs **no** corpus mount and no `dataset_handle`; it must not import
  `common.lancekit.registry`. A `tests/unit` import guard enforces that, so the decoupling is mechanical
  rather than a promise.

`services/annotations/**` — offered in the brief as a possible new service — is **not** created.

### 4. The entities

Four documents. Ids are `uuid.uuid4().hex`; ordering comes from `created_at` plus the index the project
actor maintains. (Not UUID7 as Label Studio uses: the deployed interpreter is CPython 3.13.14 —
`hasattr(uuid, "uuid7") == False` — and a ULID dependency would buy nothing, since the actor owns index
order anyway.)

#### 4.1 `AnnotationProject`

| Field | Type | Notes |
| ----- | ---- | ----- |
| `project_id` | `str` | uuid4 hex; the FGA object id (`annotation_project:<project_id>`) |
| `tenant` | `str` | the estate `project:` (tenant) this belongs to — the authz parent |
| `slug` | `str` | url-safe, unique within the tenant |
| `title`, `description` | `str` | |
| `state` | `ProjectState` | §5.1 |
| `label_schema` | `LabelSchema` | `{classes: [{name, colour, shape_types}], attributes: [...]}` — the taxonomy for this project (`#100`'s managed taxonomy plugs in here) |
| `review_required` | `bool` | default `True`. `False` ⇒ `submit` goes straight to `accepted`, no reviewer recorded |
| `lease_seconds` | `int` | default 1800 |
| `skip_policy` | `"requeue_for_others" \| "requeue_for_me" \| "terminal"` | LS's `skip_queue`, three values, default `requeue_for_others` |
| `counts` | `dict[TaskState, int]` | denormalized progress; the landing page reads only this |
| `lead_time_seconds_total` | `float` | |
| `created_at`, `created_by`, `updated_at` | | server-stamped |
| `published` | `PublishRecord \| None` | `{table_id, namespace, version, tag, publish_id, published_at, published_by}` — set once, only by the publish workflow |
| `publish_error` | `str \| None` | last saga failure, surfaced on the landing card |

#### 4.2 `Task`

| Field | Type | Notes |
| ----- | ---- | ----- |
| `task_id` | `str` | uuid4 hex |
| `project_id` | `str` | |
| `state` | `TaskState` | §5.2 — the single axis |
| `assignee` | `str \| None` | the principal holding it (`claimed` only) |
| `lease_expires_at` | `datetime \| None` | `None` while `claimed` ⇒ manager-pinned, never expires |
| `source` | `ItemSource` | §4.5 — the send capture |
| `media` | `MediaRef` | `{kind: image\|audio\|video, image_url, media_url, width, height}` — resolved at send time, the shape the zone's `MediaUnit` already uses |
| `submitted_by`, `submitted_at` | | last submission |
| `reviewed_by`, `reviewed_at`, `review_action` | | `accepted \| fix_and_accept \| request_changes` |
| `review_notes` | `list[ReviewNote]` | append-only `{by, at, action, message, shape_ids}` |
| `transitions` | `list[Transition]` | append-only `{at, by, event, from, to}` — the audit trail (LS's insert-only FSM, inlined) |
| `lead_time_seconds` | `float` | accumulated across claims |
| `skipped_reason` | `str \| None` | |

#### 4.3 `Draft` — the label payload

**One document per `(task, annotator)` holding the whole shape set as a single list.** Not a row per shape.

| Field | Type |
| ----- | ---- |
| `task_id`, `project_id`, `author` | `str` |
| `shapes` | `list[Shape]` |
| `revision` | `int` — bumped per save |
| `updated_at` | `datetime` |
| `origin` | `"human" \| "model" \| "propagated"` — a draft seeded from a prediction is marked, LS `parent_prediction` |

`Shape` = `{shape_id, shape_type (bbox|polygon|mask|segment|tag|text), x, y, width, height, rotation,
polygon, t_start, t_end, mask, label, text, attributes, group, difficult, source, model_version,
confidence}`.

This is the write-amplification fix from §1.1 made structural. Today N shapes are N Lance rows and a save is
a `merge_insert`; here a save is **one** key write with an etag. Two tabs of the same annotator cannot lose
each other's work — the etag mismatch is the 409 that `check_base_version_value` used to get from a Lance
version number.

A `fix_and_accept` writes a **second** draft authored by the reviewer. The annotator's draft is never
overwritten, so "who drew this shape" survives review. (CVAT's validators edit in place; we keep both,
because the publish table records `annotated_by` *and* `reviewed_by`.)

#### 4.4 `Assignment` — deliberately not an entity

CVAT has an `assignee` column; Label Studio has a `TaskLock` row. We need one concept, not two: the
`(assignee, lease_expires_at)` pair on the task **is** the assignment. A separate `Assignment` entity would
be a second source of truth for the same fact.

#### 4.5 Items are captures, not references

The correction the owner made. A task's `source` is a **copy taken at send time**, and nothing about the
project's correctness may depend on dereferencing it:

```
ItemSource:
  kind             "search" | "atlas" | "saved_view" | "manual" | "prediction_import"
  dataset          str            informational
  dataset_version  int | None     informational — captured at send, used ONLY in publish lineage
  key_path         str            e.g. "a1b2…/0/17" — a string the project owns a copy of
  query            str | None     the search that produced it, for provenance
  sent_at          datetime
  sent_by          str
```

The rule, stated so it can be tested:

* The project **never** joins to a governed table, never reads the corpus `annotations` table, and never
  resolves `key_path` to decide anything about state.
* A project stays valid and publishable if the source table is compacted, re-versioned, retagged, or
  dropped.
* If `media` 404s at render time, that is a **task-level** condition ("media unavailable") shown on that
  card, not a broken project.
* `dataset_version` is informational-for-lineage, never load-bearing-for-correctness. It exists because the
  catalog's write path already accepts a version-pinned `source` (§7.2) and a publish should be honest about
  what it was labelling.

Data flows **one direction each way**: predictions go lakehouse → project (imported as draft copies at send
time); labels go project → lakehouse (published). No shared mutable state in either direction. That is the
whole decoupling.

---

### 5. The state machines

#### 5.1 Project

```
draft ──open──► labeling ──freeze──► frozen ──publish──► publishing ──► published
  │                 ▲                   │                     │            │
  └──send (stays)   └──────open─────────┘                     ▼            ▼
                    send (stays)                        publish_failed  archived
                                                              │
                                                       publish (retry)
                                                              ▼
                                                          publishing
```

| From | Event | To | Who may cause it |
| ---- | ----- | -- | ---------------- |
| — | `create` | `draft` | tenant member (`can_create_annotation_project` on `project:<tenant>`) |
| `draft` | `open` | `labeling` | `can_manage` |
| `draft`, `labeling` | `send` | unchanged | `can_send_items` |
| `labeling` | `freeze` | `frozen` | `can_manage` |
| `frozen` | `open` | `labeling` | `can_manage` |
| `frozen`, `publish_failed` | `publish` | `publishing` | `can_publish` **and** `can_create_table` on the target namespace (§6.2) — and every task terminal |
| `publishing` | `publish_succeeded` | `published` | system (workflow) |
| `publishing` | `publish_failed` | `publish_failed` | system (workflow) |
| `frozen`, `published` | `archive` | `archived` | `can_manage` |

Sending into a `frozen` / `publishing` / `published` / `archived` project is rejected `409`. Everything not
in the table is illegal.

**Publish precondition, mechanical:** every task is in `{accepted, skipped}`. One task in `in_review` blocks
the publish. That is the owner's "nothing lands before that", enforced rather than described.

#### 5.2 Task — one axis, six states

```
                    ┌───────────────── lease_expired / release ──────────────┐
                    ▼                                                        │
   send ──► unassigned ──claim/assign──► claimed ──submit──► in_review ──accept/fix_and_accept──► accepted
                 ▲                          │                    │                                   │
                 │                          skip                 request_changes                     reopen
                 │                          ▼                    ▼                                   │
                 └────── requeue ────── skipped         changes_requested ◄─────────────────────────-┘
                                                                 │
                                                              claim
                                                                 ▼
                                                              claimed
```

| From | Event | To | Who may cause it |
| ---- | ----- | -- | ---------------- |
| — | `send` | `unassigned` | `can_send_items` |
| `unassigned`, `changes_requested` | `claim` | `claimed` | `can_claim`, self; sets `lease_expires_at = now + lease_seconds` |
| `unassigned`, `changes_requested` | `assign` | `claimed` | `can_manage`; `lease_expires_at = None` (pinned) |
| `claimed` | `save_draft` | `claimed` | the lease holder **only**; renews the lease |
| `claimed` | `submit` | `in_review`, or `accepted` when `review_required = False` | the lease holder only |
| `claimed` | `release` | `unassigned` | lease holder or `can_manage`; draft kept |
| `claimed` | `lease_expired` | `unassigned` | **system** (actor reminder); draft kept |
| `claimed` | `skip` | `skipped` (or `unassigned` per `skip_policy`) | the lease holder |
| `in_review` | `accept` | `accepted` | `can_review`, **and not** the task's `submitted_by` |
| `in_review` | `fix_and_accept` | `accepted` | `can_review`, not `submitted_by`; writes a reviewer-authored draft |
| `in_review` | `request_changes` | `changes_requested` | `can_review`, not `submitted_by`; appends a `ReviewNote` |
| `skipped` | `requeue` | `unassigned` | `can_manage` |
| `accepted` | `reopen` | `changes_requested` | `can_manage`, and only while the project is not in `{publishing, published, archived}` |

Rules the transition function enforces, each a test:

1. **A lease is the only claim.** Two `claim`s on one task: the second gets `409`. Enforced by the task actor
   being single-threaded, and independently by the state-store etag.
2. **Only the lease holder writes.** `save_draft` / `submit` / `skip` by anyone else → `403`, even a manager.
   A manager must `release` and re-`assign`.
3. **An expired lease loses the claim, never the work.** `lease_expired` returns the task to `unassigned`
   and leaves the draft; re-claiming re-opens it.
4. **No self-review** when `review_required` is true: `reviewer != submitted_by`, server-checked. Otherwise
   `accepted` carries no information. The single-annotator case is served honestly by
   `review_required = False`, not by winking at the identity check.
5. **Nothing escapes a published project.** Once the project is `published`, every task transition is
   rejected. Provenance is frozen with the artifact.
6. **`skip` is a decision, not a hole.** It is terminal (default `requeue_for_others` sends it back to the
   queue once, excluding the skipper — LS's default), it blocks nothing at publish, and it is *published* as
   a sentinel row so the outcome is on the record (§7.1).

---

### 6. Authorization

#### 6.1 New FGA type

`packages/service-kit/src/service_kit/governed/auth/model.fga` — one new type, parented to the estate tenant (`project`), **not** to
warehouse/namespace, because an annotation project is not lakehouse state:

```
## An annotation project = a labeling work domain owned by a tenant. Its state (tasks, claims, drafts,
## reviews) is the annotator's own and never enters the governed plane until a publish. Rungs are
## concentric: owner ⊇ manager ⊇ reviewer ⊇ annotator ⊇ viewer.
type annotation_project
  relations
    define tenant: [project]
    define owner: [user, role#assignee] or admin from tenant
    define manager: [user, role#assignee] or owner
    define reviewer: [user, role#assignee] or manager
    define annotator: [user, role#assignee] or reviewer
    define viewer: [user, role#assignee] or annotator or member from tenant
    # ---- actions ----
    define can_view: viewer
    define can_send_items: annotator
    define can_claim: annotator
    define can_annotate: annotator
    define can_review: reviewer
    define can_manage: manager
    define can_publish: manager
```

and on `project` (the tenant): `define can_create_annotation_project: member`.

Rung choices, owned: `reviewer ⊇ annotator` because a reviewer must be able to `fix_and_accept`, which is
annotating. `can_publish: manager` rather than a separate `publisher` rung — a fifth rung whose only action
is `publish` earns nothing over the manager who froze the project.

The repo's existing FGA-model contract test (`tests/unit/test_fga_model_contract.py`, per
`docs/AUTHZ.md`) already fails on a `(type, relation)` the code checks but the compiled `model.json` lacks,
so a phantom relation cannot ship. New `packages/service-kit/src/service_kit/governed/auth/model.fga.yaml` cases assert: a tenant member is a viewer but not an
annotator; an explicit annotator cannot review; a reviewer can annotate; a manager can publish.

#### 6.2 Publish is a two-door operation

This falls straight out of the owner's design and is the most important authz consequence:

* Door 1 — `can_publish` on `annotation_project:<project_id>` (the annotator's own domain).
* Door 2 — `can_create_table` on the **target namespace** (the governed plane's own rung, already on both
  `namespace` and `warehouse` in the model).
* Door 3, conditional — `can_promote` on the target namespace when it is a validator-gated medallion stage,
  reusing the existing `validator` rung exactly as stage promotion does.

Nobody can move labels into the lakehouse by holding annotator rights alone, and nobody can be forced to
publish by holding table rights alone. The crossing is explicit, which is what "its own domain, synced only
when we choose" means in authz terms.

Default target: the tenant warehouse's `silver` namespace — human labels are curated, not raw. The publish
call may name another; the doors are checked wherever it points.

---

### 7. What "publish to the lakehouse" emits

#### 7.1 The table

One **new governed table per project**, created through the catalog (`POST /v1/table/{id}/create`, Arrow-IPC
body) so the estate's existing machinery does its job: ownership seeded in FGA, a `CREATE` RunEvent emitted,
and the lineage coordinates injected into the Lance file's own schema metadata (`services/catalog/api/v1/
endpoints/data.py`). The annotator never writes Lance directly. Ever.

Grain: **one row per accepted shape**, 34 columns, matching the existing `EMPTY_SCHEMA` convention and what a
training consumer wants. A skipped task contributes exactly one sentinel row (`shape_type = "none"`,
`task_outcome = "skipped"`) so the project's *decisions* are complete on the record and a consumer can build
an exclusion set. Filter `shape_type != 'none'` for shapes; read `task_outcome` for coverage.

```python
PUBLISHED_LABELS_SCHEMA = pa.schema(
    [
        # provenance of the project (never a join key into the corpus)
        ("project_id", pa.string()),
        ("project_slug", pa.string()),
        ("publish_id", pa.string()),
        ("task_id", pa.string()),
        ("task_outcome", pa.string()),  # accepted | skipped
        # the send capture — informational strings, copied at send time
        ("item_source_kind", pa.string()),  # search | atlas | saved_view | manual | prediction_import
        ("item_dataset", pa.string()),
        ("item_key_path", pa.string()),
        # the label
        ("annotation_id", pa.string()),
        ("shape_type", pa.string()),  # bbox|polygon|mask|segment|tag|text|none
        ("x", pa.float32()),
        ("y", pa.float32()),
        ("width", pa.float32()),
        ("height", pa.float32()),
        ("rotation", pa.float32()),
        ("polygon", pa.list_(pa.float32())),
        ("t_start", pa.float32()),
        ("t_end", pa.float32()),
        ("mask", pa.string()),
        ("label", pa.string()),
        ("text", pa.string()),
        ("attributes", pa.string()),  # json
        ("group", pa.string()),
        ("difficult", pa.bool_()),
        # who made it — server-stamped, never client-claimed
        ("source", pa.string()),  # human | model | propagated
        ("model_version", pa.string()),
        ("confidence", pa.float32()),
        ("annotated_by", pa.string()),
        ("annotated_at", pa.timestamp("us", tz="UTC")),
        ("reviewed_by", pa.string()),  # '' when review_required = False
        ("reviewed_at", pa.timestamp("us", tz="UTC")),
        ("review_action", pa.string()),  # accepted | fix_and_accept | none
        ("lead_time_seconds", pa.float32()),
        ("published_at", pa.timestamp("us", tz="UTC")),
    ]
)
```

**Deliberately absent, and this is the decision, not an omission:** task state, `assignee`, leases, claim
history, drafts, revisions, review notes, transition logs, project counters. That is operational state. It
lives in the state store, it is the annotator's own, and it never enters the lakehouse.

Table properties stamped at create: `annotation.project_id`, `annotation.publish_id`,
`annotation.task_count`, `annotation.accepted_count`, `annotation.skipped_count`,
`annotation.review_required`, `annotation.label_classes`.

#### 7.2 The lineage

The catalog's `create` already emits the standard `version`, `dataSource` and `schema` dataset facets plus
the verified author (`services/catalog/core/lineage_emit.py`). The publish adds two things on top:

**A reproducibility pin.** `source=<item_dataset>` + `source_version=<captured dataset_version>` — the exact
parameters `merge_insert` already takes, which surface as `input_version` on the lineage READ edge. When a
project's items came from more than one dataset, pass **no** pin and put the full list in the run facet; a
single fabricated pin would be a lie.

**A custom run facet `annotationProject`.** Every key is a fact the project store already holds; nothing is
computed at publish time and nothing is invented. The name avoids the catalog's
`_RESERVED_RUN_FACETS = {lance, author, errorMessage, progress, parent}`, and `shape_run_facets` stamps it
spec-legal:

```json
{"annotationProject": {
  "projectId": "9f2c…", "projectSlug": "vasa-portraits", "publishId": "7a10…",
  "taskCount": 128, "acceptedCount": 124, "skippedCount": 4,
  "annotatorCount": 3, "reviewerCount": 1, "reviewRequired": true,
  "labelClasses": ["person", "ship", "signature"],
  "sourceDatasets": [{"dataset": "transcripts_v2", "version": 24, "items": 128}],
  "sendOrigins": {"search": 90, "atlas": 38},
  "leadTimeSecondsTotal": 4821.5,
  "frozenAt": "2026-07-26T09:12:00Z"
}}
```

**A version tag.** `POST /v1/table/{id}/tags/create` with `publish-<publish_id>` on the created version, so
the published artifact is addressable in the lakehouse's git-like history (`#113`) and the project's
`PublishRecord` can point at a name rather than a number.

**A control event.** `annotation.project.published` on pub/sub, carrying `{project_id, table_id, version,
tag, counts}` — the change signal the zones' `query.live` feeds subscribe to (`DESIGN-interactive-state.md`
step 3) and the notification surface (`#125`) consumes.

#### 7.3 One required catalog change

`POST /v1/table/{id}/create` accepts `mode`, `properties`, `data_base`, `authorization` — and **not**
`source`, `source_version` or `X-Lance-Run-Facets`. Only `merge_insert` takes those
(`services/catalog/api/v1/endpoints/data.py`). So today a publish can carry no pin and no run facet.

**Decision: extend `create`** with `source`, `source_version` and the `X-Lance-Run-Facets` header, reusing
`_merge_source_pin` and `_parse_run_facets` verbatim. It is a handful of lines against helpers that already
exist and are already tested; it keeps the catalog as the estate's single lineage emitter. The alternatives
are worse: creating an empty table and then `merge_insert`ing the rows puts a meaningless version in the
governed history, and emitting the facet from the annotator directly gives the estate a second emitter for
the same write.

#### 7.4 Idempotency

Dapr workflow activities run **at least once**, so the create activity must be idempotent: `POST
/{id}/exists` first; absent → `create`; present → compare the `annotation.publish_id` property and either
no-op (same publish, a replay) or fail loudly (a different publish already occupies that name). A retry after
`publish_failed` reuses the same `publish_id`, so a replay is a no-op rather than a second table.

---

### 8. Implementation plan — slices, ordered

Ordered so each slice is worth landing on its own, and so the fence is visible: **`S1`–`S4` need no state
store, no chart change and no cluster access; `S5`–`S10` are `#124`.** `S1`–`S4` share no files and can land
in any order or in parallel. Every slice names the test that must be red before the code is written.

#### `S1` — the domain core (no store)

**Lands.** `services/annotator/projects/{__init__,schema,machine}.py` — the Pydantic entities of §4 and one
pure function `apply(entity, event, *, actor, rungs, now) -> Entity` raising a `DomainError` subclass
(`IllegalTransition` → 409, `NotLeaseHolder` → 403, `SelfReview` → 403). Plus
`tests/unit/test_annotation_projects_machine.py`.

**Useful alone.** The transition tables of §5 stop being prose. Every later slice calls one function instead
of re-deriving the rules per endpoint, and the illegal-pair matrix becomes a spec that cannot rot.

**Red first.** Parametrized over the full cartesian product `TaskState × TaskEvent` — 6 states × the 12
post-creation events of §5.2 (`claim`, `assign`, `save_draft`, `submit`, `release`, `lease_expired`, `skip`,
`accept`, `fix_and_accept`, `request_changes`, `requeue`, `reopen`) = **72 pairs, of which the table admits
14** — and the same treatment for `ProjectState × ProjectEvent`. The 14 legal pairs assert the target state
*and* the field effects (lease set on `claim`, `None` on `assign`, a `Transition` appended, `counts` moved);
the other 58 assert `IllegalTransition`. A pair silently doing nothing is the failure mode this catches: the
matrix has no "unspecified" cell. Then the six rules of §5.2 as named tests — a second `claim` → 409;
`save_draft` by a non-holder → 403 even for a manager; `lease_expired` keeps the draft; `reviewer ==
submitted_by` → 403 while `review_required`; any task event on a `published` project → 409; `skip` under each
of the three `skip_policy` values. Plus the decoupling guard from §3: import `services.annotator.projects`
in a subprocess and assert no `common.lancekit` module is in `sys.modules` — red the moment someone reaches
for the registry.

**Blocked on.** Nothing.

#### `S2` — the FGA type (no store)

**Lands.** `packages/service-kit/src/service_kit/governed/auth/model.fga` (the `annotation_project` type of §6.1 + `define
can_create_annotation_project: member` on `project`), the regenerated `model.json`
(`fga model transform --file packages/service-kit/src/service_kit/governed/auth/model.fga`), and `model.fga.yaml` — its inline model copy
plus new check cases.

**Useful alone.** The doors are gradeable before an endpoint exists: `fga model test` and the repo's
contract test do the grading, so the privilege math is settled before any handler can get it wrong.

**Red first.** No new test is needed for the sync — `tests/unit/test_fga_model_contract.py` already carries
it, and its own message is the proof: `model.json is STALE — regenerate: fga model transform --file
model.fga` (line 318) with a sibling assertion `model.fga.yaml's inline model drifted from
model.fga/model.json` (line 319). Editing `model.fga` alone reddens both; that is how we know the gate is
live rather than assumed. New `model.fga.yaml` cases to add: a tenant `member` is a `viewer` but **not** an
`annotator`; an explicit `annotator` cannot `can_review`; a `reviewer` **can** `can_annotate`; a `manager`
`can_publish`; a member of a *different* tenant resolves nothing.

**Blocked on.** Nothing.

#### `S3` — the publish shape (no store)

**Lands.** `services/annotator/projects/publish.py` — `PUBLISHED_LABELS_SCHEMA` (§7.1) and a pure
`build_published_table(project, tasks, drafts) -> pa.Table`. Plus
`tests/unit/test_annotation_publish_table.py`.

**Useful alone.** It is the contract a training consumer reads, and it can be reviewed and frozen before a
writer exists. It also turns §7.1's "deliberately absent" paragraph into a fact about a schema.

**Red first.** A fixture project — two accepted tasks (3 shapes and 1 shape) and one skipped task — must
build exactly 5 rows; the skipped task must be exactly one row with `shape_type == "none"` and
`task_outcome == "skipped"`; an empty project must build 0 rows with a schema-identical empty table (so
`create` never receives a schemaless stream); `reviewed_by == ""` when `review_required` is false, never
`None`. And one anti-regression assertion: the field set intersects none of `{state, assignee,
lease_expires_at, revision, review_notes, transitions}` — red the moment somebody helpfully exports task
state into the lakehouse.

**Blocked on.** Nothing.

#### `S4` — `create` carries the pin and the facet (no store)

**Lands.** `services/catalog/api/v1/endpoints/data.py` — `create_table` gains `source`, `source_version` and
the `X-Lance-Run-Facets` header, reusing `_merge_source_pin` (line 383) and `_parse_run_facets` (line 401)
verbatim; the docstring change flows into `docs/catalog-openapi.json` via `make openapi`.

**Useful alone, and this is the point:** the asymmetry exists today independent of `#122`. `merge_insert`
(line 445) accepts a version-pinned `source` and a custom run facet; `create` accepts `mode`, `properties`,
`data_base`, `authorization` and nothing else. So **every first write of a derived table** — a Ray job's
output, an export, a publish — is emitted today with no reproducibility pin. Closing it is a lineage fix that
stands on its own.

**Red first.** Following the repo's unit convention (call the handler directly with a fake emitter, assert on
the captured RunEvent, `pytest.raises(InvalidInputError)` for the 400s — as `tests/unit/test_insert_coerce.py`
does): create with `source=…, source_version=3` and a `X-Lance-Run-Facets` header of
`{"annotationProject":{…}}`; assert the emitted RunEvent carries `input_version == 3` on the READ edge and
that the custom facet survives `shape_run_facets` un-renamed; assert `source_version` without `source` raises
`InvalidInputError` (`_merge_source_pin`'s existing rule, now reachable from `create`); assert a name in
`_RESERVED_RUN_FACETS` is rejected. Against today's handler the parameters do not exist, so the call is a
`TypeError` before it is a lineage assertion — that is the red, and it goes green only when the pin and the
facet actually reach the event.

**Gates.** `uv run pytest tests/unit -q`, `uv run ruff check`, `uv run ruff format --check`,
`uv run ty check`, `make openapi` (docstrings feed the spec — drift fails CI).

**Blocked on.** Nothing.

---

**The fence — and it has MOVED.** As written, everything below needed `#124` because there was no state
store in the cluster (§1.2). There is one now, so the fence is no longer at `S5`; it is at `S6`. Dapr
workflow still uses the actor framework internally, so the same `actorStateStore: "true"` flag continues to
gate `S6` and `S8` — but the flag is on, and the store beneath it is proven.

#### `S5` — the state store — **DONE** *(corrected 2026-07-28)*

~~A `state.redis`-compatible component with `actorStateStore: "true"` plus its backing store, in `chart/`.
Blocked twice over: the component does not exist, and `chart/` is owned by another workstream.~~

`lance-statestore` is live: `state.postgresql` on the already-deployed AGE Postgres, DSN resolved from
OpenBao through the `lance-secrets` Dapr secret store, `actorStateStore: "true"`, scoped to `catalog` and
`annotator` (`chart/templates/dapr-statestore.yaml`, `chart/values.yaml` → `stateStore`). Postgres rather
than Redis, decided on properties rather than preference — see `docs/DESIGN-interactive-state.md`: durable
workflow that loses its state on a pod restart is not durable, and `maxmemory-policy` is server-wide, so one
Redis cannot safely be both an evicting cache and an actor store.

It is proven by **three** consumers, not one: `workflow-graph` and `saved-views` (the media zone's canvas
and searches) and `dock-layout` (every zone's dock workbench arrangement), all per-subject and all
round-tripping through the sidecar. Whoever picks up `S6` should read
`packages/service-kit/src/service_kit/governed/user_state.py` first — the key rules, the fail-closed
posture, and the `||` separator trap are already solved there.

#### `S6` — repositories and actors — **blocked on `S5`**

`ProjectActor` (project document, claimable queue, tenant index) and `TaskActor` (task state + the
lease-expiry **reminder**). Single-threaded per entity is what makes §5.2 rule 1 true. Without actors the
queue index is a lost-update race and lease expiry needs a sweeper cron — two bugs bought to avoid one
component. Proof when it lands: two concurrent claims, one 200 one 409; a reminder fires and returns the task
to `unassigned` with its draft intact.

#### `S7` — the HTTP surface — **blocked on `S6`**

Project/task/draft endpoints behind the §6.1 doors, with `apply()` from `S1` as the only mutator and the
draft etag as the only concurrency control. The first slice a human can drive.

#### `S8` — the publish workflow — **blocked on `S5` (+ `S3`, `S4`)**

freeze → snapshot accepted drafts → build Arrow → `exists`/`create` → tag → record → emit
`annotation.project.published`, durable across a pod restart, idempotent per §7.4. Also the annotator's first
pub/sub usage (it has zero Dapr references today), which is what unblocks `query.live` for `#102` and gives
`#125` a source. Proof: kill the worker mid-workflow, restart, assert one table and one tag — not two.

#### `S9` — the zone — **blocked on `S7`**

Projects landing replaces the `DataSelection.svelte` gallery; send-to-project from search/atlas; the canvas
reads and writes drafts; `statusStyle.ts`'s four statuses become the six `TaskState` values;
`e2e/zone.spec.ts:131` becomes a projects-landing assertion. Screenshots, and looked at.

#### `S10` — the deletions of §9 — **blocked on `S7` + `S8` proven live**

The slice that pays: `annotations/{save,tags,versions}.py` and `check_base_version_value` go, and **615 Lance
versions for 3 rows becomes one Lance version per publish.** Deleting the write path before the replacement
is driven would be the same mistake in the other direction, which is why it is last and not first.

---

### 9. Consequences for the code that exists

Backward compatibility does not matter here, so these are deletions, not deprecations.

| Today | After |
| ----- | ----- |
| `POST /api/annotations/{doc}/{speech}/{chunk}` → `merge_insert` into Lance (`annotations/save.py`) | **Deleted.** Replaced by `save_draft` into the project store. Lance is written only by a publish. |
| `annotations/tags.py` — batch chunk tags as Lance rows across many units | **Deleted.** A bulk tag is a bulk `save_draft` across tasks. |
| `annotations/versions.py` — per-unit Lance version history, the "who/when" audit trail | **Deleted.** It was the audit trail of a write plane that no longer exists. The audit trail becomes the task's append-only `transitions` log plus the publish tag in the lakehouse. (This also retires `#99`: catalog-mode history returning `[]`.) |
| `annotations/commit.py:check_base_version_value` — optimistic concurrency against a Lance version | **Replaced** by the state-store etag on the draft document. |
| `GET /api/annotations/…` Arrow-IPC read | **Kept**, but it serves *published* tables. A task's in-flight shapes render from its draft. |
| `api/v1/endpoints/jobs.py` — batch derivers | **Kept.** Model predictions are analytical writes by a Ray deriver; they arrive in a project as imported draft copies (`origin = "model"`, `ItemSource.kind = "prediction_import"`). |
| `DataSelection.svelte` gallery landing + `?keys=` as the only selection | **Replaced.** Landing = your projects and their progress. `send` from search/atlas/saved view creates tasks in a named project. The e2e assertion `'landing = data selection: dataset → document → chunk → the annotate canvas'` (`e2e/zone.spec.ts:131`) becomes a projects-landing assertion. |
| `statusStyle.ts` statuses `accepted / rejected / prediction / reviewed` | **Replaced** by the six `TaskState` values, so the chip and the state machine cannot disagree. |

The payoff, in the units of §1.1: **615 Lance versions for 3 rows becomes one Lance version per publish.**

---

### 10. Named gaps

Recorded rather than hidden, each with what would unblock it:

* **The annotator service has no verified subject** *(found 2026-07-28 — decide this BEFORE `S6`)*.
  Every entity in §4 is keyed on who owns or claims it, and `services/annotator` cannot answer that:
  it builds no `OIDCVerifier`, and its `get_author` (`service_kit/media/deps.py`) reads a **trusted
  `X-User` header** defaulting to `"anon"`. Task assignment keyed on a client-settable header is the
  exact cross-user leak the per-subject user-state routes were built to prevent — which is precisely
  why those routes live in the **catalog** and not here (`catalog/api/v1/endpoints/user_state.py`
  states the reasoning in full). Two ways out, and they are not equivalent: host `ProjectActor` /
  `TaskActor` in the catalog, which already has `CurrentToken` and is already in the state store's
  `scopes`; or give the annotator a verifier of its own, which is more work but keeps the domain where
  §3 put it. Unblocked by picking one. Do not build `S6` against `X-User`.
* **Consensus / multi-annotator overlap** (LS `overlap` + `precomputed_agreement`, CVAT consensus replicas +
  majority-vote merge). Out of v1. Unblocked by a real user needing agreement metrics; costs an agreement
  metric, a merge algorithm, and `Draft` going from one-per-task to N-per-task.
* **Positioned review issues** (CVAT `Issue(frame, position)`). Flattened to `ReviewNote.shape_ids`.
  Unblocked by canvas support for pinning a comment to a coordinate.
* **Honeypot / ground-truth jobs** (CVAT `JobType.ground_truth`, LS `Annotation.ground_truth`). Not modelled.
  Unblocked by wanting automatic annotator scoring.
* **Export serializers** (COCO / YOLO / CSV / HF — `#100`). The published Lance table is the single source; a
  serializer reads it. Not part of publish.
* **Active learning** (`Draft.origin = "model"`, `confidence`, `uncertainty`). The columns and the
  `prediction_import` send path exist in this design; the ranking loop does not.
