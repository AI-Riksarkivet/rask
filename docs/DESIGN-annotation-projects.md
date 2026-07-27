# Annotation projects — the task domain, its own state, and what publishing emits

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

## Status — what is decided, what is built

**Decided and durable:** everything in §2–§10. The entities, the two state machines with their transition
tables, the authz doors, the publish contract, and the slice plan.

**Built: nothing yet.** No code landed with this document, and that is deliberate rather than unfinished:

* §8 slices `S1`–`S4` are implementable today — no state store, no chart change, real tests. They land in
  `services/annotator/projects/`, `services/common/auth/`, `services/catalog/api/v1/endpoints/data.py` and
  `tests/unit/`. The run that produced this document was scoped to `docs/**` (plus a new service under
  `services/annotations/**`, which §3 refuses), so none of those four could be written here. They are
  specified down to the file, the test and the failure message, so the next run is transcription, not design.
* §8 slices `S5`–`S10` are blocked on `#124` for a reason no amount of effort routes around: **there is no
  store in the cluster to put a project in** (§1.2), and shipping the HTTP surface or the landing page on a
  volatile store would manufacture exactly the false "done" the owner has rejected before.

The one thing this document must not become is code written to look busier than it is. §1.1 earns its keep on
its own: the measurement kills the design we would otherwise have shipped.

---

## 1. What exists today, measured

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

### 1.1 The measurement that decides the store

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

### 1.2 What the cluster has to hold it instead — nothing, yet

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

## 2. References — how real labeling platforms model this

Cited from source and docs, not invented.

### Label Studio (HumanSignal)

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

### CVAT

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

### What we take, and what we refuse

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

## 3. Decision: where the domain lives

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

## 4. The entities

Four documents. Ids are `uuid.uuid4().hex`; ordering comes from `created_at` plus the index the project
actor maintains. (Not UUID7 as Label Studio uses: the deployed interpreter is CPython 3.13.14 —
`hasattr(uuid, "uuid7") == False` — and a ULID dependency would buy nothing, since the actor owns index
order anyway.)

### 4.1 `AnnotationProject`

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

### 4.2 `Task`

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

### 4.3 `Draft` — the label payload

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

### 4.4 `Assignment` — deliberately not an entity

CVAT has an `assignee` column; Label Studio has a `TaskLock` row. We need one concept, not two: the
`(assignee, lease_expires_at)` pair on the task **is** the assignment. A separate `Assignment` entity would
be a second source of truth for the same fact.

### 4.5 Items are captures, not references

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

## 5. The state machines

### 5.1 Project

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

### 5.2 Task — one axis, six states

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

## 6. Authorization

### 6.1 New FGA type

`services/common/auth/model.fga` — one new type, parented to the estate tenant (`project`), **not** to
warehouse/namespace, because an annotation project is not lakehouse state:

```
# An annotation project = a labeling work domain owned by a tenant. Its state (tasks, claims, drafts,
# reviews) is the annotator's own and never enters the governed plane until a publish. Rungs are
# concentric: owner ⊇ manager ⊇ reviewer ⊇ annotator ⊇ viewer.
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
so a phantom relation cannot ship. New `services/common/auth/model.fga.yaml` cases assert: a tenant member is a viewer but not an
annotator; an explicit annotator cannot review; a reviewer can annotate; a manager can publish.

### 6.2 Publish is a two-door operation

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

## 7. What "publish to the lakehouse" emits

### 7.1 The table

One **new governed table per project**, created through the catalog (`POST /v1/table/{id}/create`, Arrow-IPC
body) so the estate's existing machinery does its job: ownership seeded in FGA, a `CREATE` RunEvent emitted,
and the lineage coordinates injected into the Lance file's own schema metadata (`services/catalog/api/v1/
endpoints/data.py`). The annotator never writes Lance directly. Ever.

Grain: **one row per accepted shape**, 34 columns, matching the existing `EMPTY_SCHEMA` convention and what a
training consumer wants. A skipped task contributes exactly one sentinel row (`shape_type = "none"`,
`task_outcome = "skipped"`) so the project's *decisions* are complete on the record and a consumer can build
an exclusion set. Filter `shape_type != 'none'` for shapes; read `task_outcome` for coverage.

```python
PUBLISHED_LABELS_SCHEMA = pa.schema([
    # provenance of the project (never a join key into the corpus)
    ("project_id",        pa.string()),
    ("project_slug",      pa.string()),
    ("publish_id",        pa.string()),
    ("task_id",           pa.string()),
    ("task_outcome",      pa.string()),     # accepted | skipped
    # the send capture — informational strings, copied at send time
    ("item_source_kind",  pa.string()),     # search | atlas | saved_view | manual | prediction_import
    ("item_dataset",      pa.string()),
    ("item_key_path",     pa.string()),
    # the label
    ("annotation_id",     pa.string()),
    ("shape_type",        pa.string()),     # bbox|polygon|mask|segment|tag|text|none
    ("x",                 pa.float32()),
    ("y",                 pa.float32()),
    ("width",             pa.float32()),
    ("height",            pa.float32()),
    ("rotation",          pa.float32()),
    ("polygon",           pa.list_(pa.float32())),
    ("t_start",           pa.float32()),
    ("t_end",             pa.float32()),
    ("mask",              pa.string()),
    ("label",             pa.string()),
    ("text",              pa.string()),
    ("attributes",        pa.string()),     # json
    ("group",             pa.string()),
    ("difficult",         pa.bool_()),
    # who made it — server-stamped, never client-claimed
    ("source",            pa.string()),     # human | model | propagated
    ("model_version",     pa.string()),
    ("confidence",        pa.float32()),
    ("annotated_by",      pa.string()),
    ("annotated_at",      pa.timestamp("us", tz="UTC")),
    ("reviewed_by",       pa.string()),     # '' when review_required = False
    ("reviewed_at",       pa.timestamp("us", tz="UTC")),
    ("review_action",     pa.string()),     # accepted | fix_and_accept | none
    ("lead_time_seconds", pa.float32()),
    ("published_at",      pa.timestamp("us", tz="UTC")),
])
```

**Deliberately absent, and this is the decision, not an omission:** task state, `assignee`, leases, claim
history, drafts, revisions, review notes, transition logs, project counters. That is operational state. It
lives in the state store, it is the annotator's own, and it never enters the lakehouse.

Table properties stamped at create: `annotation.project_id`, `annotation.publish_id`,
`annotation.task_count`, `annotation.accepted_count`, `annotation.skipped_count`,
`annotation.review_required`, `annotation.label_classes`.

### 7.2 The lineage

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

### 7.3 One required catalog change

`POST /v1/table/{id}/create` accepts `mode`, `properties`, `data_base`, `authorization` — and **not**
`source`, `source_version` or `X-Lance-Run-Facets`. Only `merge_insert` takes those
(`services/catalog/api/v1/endpoints/data.py`). So today a publish can carry no pin and no run facet.

**Decision: extend `create`** with `source`, `source_version` and the `X-Lance-Run-Facets` header, reusing
`_merge_source_pin` and `_parse_run_facets` verbatim. It is a handful of lines against helpers that already
exist and are already tested; it keeps the catalog as the estate's single lineage emitter. The alternatives
are worse: creating an empty table and then `merge_insert`ing the rows puts a meaningless version in the
governed history, and emitting the facet from the annotator directly gives the estate a second emitter for
the same write.

### 7.4 Idempotency

Dapr workflow activities run **at least once**, so the create activity must be idempotent: `POST
/{id}/exists` first; absent → `create`; present → compare the `annotation.publish_id` property and either
no-op (same publish, a replay) or fail loudly (a different publish already occupies that name). A retry after
`publish_failed` reuses the same `publish_id`, so a replay is a no-op rather than a second table.

---

## 8. Implementation plan — slices, ordered

Ordered so each slice is worth landing on its own, and so the fence is visible: **`S1`–`S4` need no state
store, no chart change and no cluster access; `S5`–`S10` are `#124`.** `S1`–`S4` share no files and can land
in any order or in parallel. Every slice names the test that must be red before the code is written.

### `S1` — the domain core (no store)

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

### `S2` — the FGA type (no store)

**Lands.** `services/common/auth/model.fga` (the `annotation_project` type of §6.1 + `define
can_create_annotation_project: member` on `project`), the regenerated `model.json`
(`fga model transform --file services/common/auth/model.fga`), and `model.fga.yaml` — its inline model copy
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

### `S3` — the publish shape (no store)

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

### `S4` — `create` carries the pin and the facet (no store)

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

**The fence.** Everything below needs `#124`. Not "would be nicer with" — needs. There is no state store in
the cluster (§1.2), so there is nowhere to put a project, and Dapr workflow uses the actor framework
internally, so the one `actorStateStore: "true"` flag gates `S6` and `S8` as well as `S5`.

### `S5` — the state store — **blocked on `#124`**

A `state.redis`-compatible component with `actorStateStore: "true"` plus its backing store, in `chart/`.
Blocked twice over: the component does not exist, and `chart/` is owned by another workstream. Useful beyond
this design — `#102`'s `query.live` and the atlas/lineage read caches want the same store.

### `S6` — repositories and actors — **blocked on `S5`**

`ProjectActor` (project document, claimable queue, tenant index) and `TaskActor` (task state + the
lease-expiry **reminder**). Single-threaded per entity is what makes §5.2 rule 1 true. Without actors the
queue index is a lost-update race and lease expiry needs a sweeper cron — two bugs bought to avoid one
component. Proof when it lands: two concurrent claims, one 200 one 409; a reminder fires and returns the task
to `unassigned` with its draft intact.

### `S7` — the HTTP surface — **blocked on `S6`**

Project/task/draft endpoints behind the §6.1 doors, with `apply()` from `S1` as the only mutator and the
draft etag as the only concurrency control. The first slice a human can drive.

### `S8` — the publish workflow — **blocked on `S5` (+ `S3`, `S4`)**

freeze → snapshot accepted drafts → build Arrow → `exists`/`create` → tag → record → emit
`annotation.project.published`, durable across a pod restart, idempotent per §7.4. Also the annotator's first
pub/sub usage (it has zero Dapr references today), which is what unblocks `query.live` for `#102` and gives
`#125` a source. Proof: kill the worker mid-workflow, restart, assert one table and one tag — not two.

### `S9` — the zone — **blocked on `S7`**

Projects landing replaces the `DataSelection.svelte` gallery; send-to-project from search/atlas; the canvas
reads and writes drafts; `statusStyle.ts`'s four statuses become the six `TaskState` values;
`e2e/zone.spec.ts:131` becomes a projects-landing assertion. Screenshots, and looked at.

### `S10` — the deletions of §9 — **blocked on `S7` + `S8` proven live**

The slice that pays: `annotations/{save,tags,versions}.py` and `check_base_version_value` go, and **615 Lance
versions for 3 rows becomes one Lance version per publish.** Deleting the write path before the replacement
is driven would be the same mistake in the other direction, which is why it is last and not first.

---

## 9. Consequences for the code that exists

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

## 10. Named gaps

Recorded rather than hidden, each with what would unblock it:

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
