# open-projects — the word "project" means three things, one of them is dead, and tenant membership has no door

Working notes, **2026-08-09**. Unsettled; this file is deleted when the decisions land. `docs/` is
for settled architecture only.

**Why this exists.** A question — "what actually allows creating a project today, and how does that
map to the unused controlplane" — turned into a walk of the whole tenancy plane. What came back is
one dead service, one genuine authorization gap, and a set of naming collisions that make the plane
hard to reason about even when it is working. Written for an adversarial re-read: every claim
carries its evidence, and §7 lists what I did **not** check so the next session attacks the right
things.

**Evidence convention** (same markers as `open_dapr.md`, and they are not interchangeable):

- `path:line` — **read from source** at `HEAD` on `claude/notifications-service-separation-h9bi73`
  (= `main`, `63b04c8`). Read, not executed.
- `(live)` — **measured against a running estate.** I had no cluster in this session; the only
  `(live)` marker below is a measurement someone else recorded in `HANDOFF-lakehouse.md`, and it is
  attributed there.
- `UNVERIFIED` — inference, arithmetic, or an estimate. Never treat one as a measurement.
- **Severity** — my judgement, argued in the entry. Argue back.

**Scope.** Everything reachable from the noun "project": the catalog tenant plane, the controlplane
k8s read-view, the annotator's labeling projects, the FGA `type project` and its create-on-parent
doctrine, plus the two `transaction` questions that came out of the same model audit (§5, §6).

---

## 1 · Three unrelated things are called "project"

They share no code, no storage, no identity, and no transport. Nothing joins any two of them.

| | **Tenant** | **k8s Project CR** | **Labeling project** |
| --- | --- | --- | --- |
| Owner | `services/catalog` | `services/controlplane` | `services/annotator` |
| Identity | `project:<id>` in OpenFGA + a registry record on S3 | a cluster CR, `platform.rask.io/v1alpha1` | annotator's own store, tenant-scoped |
| Created by | `POST /v1/projects` (`projects.py:174`) | **nothing in this repo** (§3) | `POST /projects` on the annotator |
| Read by | home `/projects` gallery via `/capi/v1/projects` (`gallery.ts:73`) | **nothing** (§3.2) | annotator + explorer zones |
| In the FGA model | `type project` (`model.fga:55`) | absent | `type annotation_project` (`model.fga:207`) |

`type project` in the model means the **first column only**. The `annotation_project` type hangs off
it (`model.fga:207`, "hangs off `project` (the tenant)"), so tenants and labeling projects are
parent/child — but the k8s CR is unrelated to both.

**The collision is not cosmetic.** The two public HTTP surfaces sit adjacent in the same URL
namespace and mean different things:

```
GET  /api/projects/            → controlplane   → k8s CRs        (unauthenticated, §3.1)
POST /api/catalog/v1/projects  → catalog        → mints a TENANT (OIDC + FGA gated)
```

Both are `/api/…projects` at the same ingress. One is the estate's most privileged write; the other
is a defunct read of cluster topology.

---

## 2 · What actually creates a tenant

`services/catalog/src/catalog/api/v1/endpoints/projects.py:174`, `create_project`. The payload is
just `{"id": "..."}` — `CreateProjectRequest` (`:168`) has one field, and its docstring is the
design in one line: *"a tenant is a name, its admins, and what it later holds."*

Five steps, in this order:

1. **Shape** — `_ID_RE.match(project_id)` (`:190`). `_ID_RE` is `CONTROL_ID_RE`
   (`core/identifiers.py:33`), DNS-safe, 3–63 chars, anchored with `\Z` not `$`. The anchor is
   load-bearing and the comment says why: `"acme\n"` satisfied a `$`-anchored copy, became a
   registry filename, and then failed the FGA tuple write because OpenFGA rejects whitespace in an
   object id — landing exactly the record-without-tuples drift the design exists to prevent
   (`identifiers.py:24-28`).
2. **Gate** — `require_relation(..., relation="can_observe_events", obj=settings.fga_root_object)`
   (`:192`). `fga_root_object` defaults to **`warehouse:lance_catalog`** (`core/config.py:225`) and
   `can_observe_events: owner` is defined on `type warehouse` (`model.fga:90`). So the door is
   *owner of the estate root*. See §4.3 and §4.4 — both the name and the type are problems.
3. **Registry record** — `project_registry.put_project` via `run_in_threadpool` (`:204`), carrying
   `created_at` / `created_by` / `protected` forward from any existing record so an idempotent
   re-POST never silently strips deletion protection (`:196-203`).
4. **Seed the creator as admin** — `seed_project_admin` (`fga_deps.py:609`) writes exactly one
   tuple: `user:<sub> admin project:<id>` (`:628`). That tuple is what makes the tenant
   self-sustaining; every later warehouse/namespace/table grant cascades from it.
5. **Emit + audit** — a control event always, an `access_grant` audit line **only when the tuple was
   really written** (`seed_project_admin` returns `False` on an FGA-off stack, `:624`).

Steps 3 and 4 are deliberately one operation: existence and permission are minted together and
cannot drift.

### 2.1 The read and delete sides

- **List / get** — `GET /v1/projects`, `GET /v1/projects/{id}` (`:235`, `:257`). Estate-observer
  gated, annotated with effective admins from `list_users` on `project:<id>#can_administer`, which
  degrades to `[]` rather than 500-ing when OpenFGA is slow (`:144-151`).
- **Delete** — `DELETE /v1/projects/{id}` (`:280`). Gated on **`project:<id>#can_administer`** — the
  tenant's own admin bar, *not* the estate gate the create uses. Order is fail-closed and each step
  earns the next: shape → existence → authz → deletion protection → emptiness → revoke tuples →
  drop record → audit. No `cascade` parameter at all, by ruling.

### 2.2 Three transports, none of them the gateway row named `/api/projects`

| Path | Reaches | Methods | Credential |
| --- | --- | --- | --- |
| `/api/catalog/v1/projects` | catalog, via the gateway row `("/api/catalog", "", *catalog)` (`gateway/__init__.py:141`) | all | caller's own bearer |
| `/capi/v1/projects` | catalog, via home's BFF proxy (`routes/capi/v1/projects/+server.ts`) | **GET only** | session bearer attached server-side |
| `catalogJSON('/v1/projects', {method:'POST'})` | catalog **directly** at `CATALOG_API`, from a home remote function (`lib/remote/warehouses.remote.ts:108`, `lib/server/catalog-fetch.ts:31`) | all | session bearer |

The BFF is GET-only on purpose — the confused-deputy stance, "no blanket write proxy"
(`bff.ts:330-333`). Writes therefore go through SvelteKit remote functions, which are server-side
and attach the same session bearer under an explicit per-operation contract. That is coherent. What
is worth noticing is that **the gateway row is not**: `/api/catalog/*` proxies every method straight
to the catalog from the public ingress. That is not a bypass of the BFF guard (a browser hitting it
carries no session bearer, so the catalog 401s), but it does mean the catalog's full write surface
is internet-facing and its safety rests entirely on the catalog's own OIDC + FGA. **UNVERIFIED**
whether that is intended; it is not written down anywhere I found.

---

## 3 · `services/controlplane` is dead on both ends

Three routes' worth of service, wired end to end, that cannot work and that nothing calls.

**What it is.** One route: `GET /projects/` (`controlplane/routes.py:36`), calling
`K8sProjectReader.list_projects()` — a `list_cluster_custom_object` against
`platform.rask.io/v1alpha1 projects` (`k8s.py:33`) — returning
`ProjectDTO(slug, name, team, workload, phase, namespace, url, created_at)`. Deployed by
`chart/templates/controlplane.yaml` with its own ServiceAccount, ClusterRole, ClusterRoleBinding,
Dapr sidecar and probes; `controlplane.enabled: true` by default (`values.yaml:434`). Gateway row at
`gateway/__init__.py:155`. Typed client at `frontend/packages/api/src/projects.ts`.

### 3.1 — P1 · The route is unauthenticated and publicly routed · **Severity: Medium (latent)**

The chain, each link read:

- `make_service_app` (`service-kit/__init__.py:90-145`) wires config, handlers, middleware, slash
  tolerance and OTel. It applies **no auth dependency** and exposes no parameter for one.
- `controlplane/__init__.py:8` — `make_service_app(title="controlplane", routers=[...])`. No
  `dependencies=`.
- `routes.py:36-37` — `def list_projects(reader: ReaderDep)`. The only dependency is the k8s reader.
- The gateway is a pure path-router; its sole middleware is `lineage_sidecar_guard`
  (`gateway/__init__.py:274`). No auth.
- `chart/templates/ingress.yaml:66` — `- path: /api`, `pathType: Prefix`, backend = the gateway
  service. North-south, no auth annotation on that rule.

So `GET https://<host>/api/projects/` is anonymous, and would return cluster namespaces, workload
names, lifecycle phases and per-project ingress hostnames.

Rated **latent** rather than High only because of P2: with no CRD registered the endpoint cannot
return data. That is safety by accident. If anyone ever installs the operator, this starts serving
cluster topology to the internet with no code change and no signal. Compare every other public
surface in the estate, all of which carry an explicit door.

### 3.2 — P2 · There is no producer: the CRD is defined nowhere · **Severity: High (it is broken now)**

`platform.rask.io` appears in exactly three places in the repo:

```
services/controlplane/src/controlplane/k8s.py:11   PROJECT_GROUP = "platform.rask.io"
services/controlplane/src/controlplane/k8s.py:40   label_selector="platform.rask.io/project"
chart/templates/controlplane.yaml:92               apiGroups: ["platform.rask.io"]
```

No CRD manifest, no operator, no controller, no CR sample. `chart/crds/` holds the CNPG CRDs only.
So `list_cluster_custom_object` gets a 404, which is an `ApiException`, which is in `_K8S_ERRORS`
(`routes.py:22`), which becomes `503 cannot reach kubernetes api` (`routes.py:43`).

**This is already the observed behaviour.** `HANDOFF-lakehouse.md:101-106` records
`fetch('/api/projects') -> 503 {"detail":"cannot reach kubernetes api"}` **(live**, measured in a
browser against the deployed estate by whoever wrote that handoff**)**, with the downstream symptom
that no active project can be selected anywhere and the navbar renders its placeholder as a link
(`href="/projects/Select project"`, literal space).

**That handoff attributes the 503 to ServiceAccount/RBAC. I think that attribution is wrong**, and
the mechanism that makes it wrong is itself a finding: `_K8S_ERRORS` collapses an RBAC 403 and a
missing-CRD 404 into the same `ApiException`, mapped to the same 503 with the same message. The two
are indistinguishable from outside. The RBAC in `controlplane.yaml:84-112` reads correct — SA,
ClusterRole on `platform.rask.io/projects` and `networking.k8s.io/ingresses`, ClusterRoleBinding
into `.Release.Namespace`, and `serviceAccountName` set on the pod. There is nothing obviously wrong
to fix, which is consistent with "the group does not exist" and not with "permission denied".
**UNVERIFIED** — settling it needs one `kubectl get crd | grep platform.rask.io` against a live
cluster, or the daprd/uvicorn log line carrying the actual status code.

### 3.3 — P3 · There is no consumer either · **Severity: Low (dead code)**

`listProjects` (`frontend/packages/api/src/projects.ts:21`) is imported by exactly one file:

```
frontend/microfrontends/home/src/lib/remote/home.remote.ts:2   import { listProjects } from '@rask/api';
frontend/microfrontends/home/src/lib/remote/home.remote.ts:8   export const getProjects = query(...)
```

and `getProjects` is imported by **nothing** — grep over all of `frontend` excluding `node_modules`
returns that one definition line and no call site.

Home's actual `/projects` surface does not use it. `routes/projects/+page.server.ts:14` calls
`loadGallery`, which fetches **`/capi/v1/projects`** (`lib/gallery.ts:73`) — the catalog. The
navbar's `Projects` entry (`ui/src/lib/shell/nav-config.ts:346`) points at that same `/projects`
route.

So the read-view was built for a picker that was later replaced by the catalog-backed gallery, and
the old path was never removed.

### 3.4 — P4 · The name is actively misleading · **Severity: Low, but it costs sessions**

The service called `controlplane` does not control the tenancy plane it sounds like it owns. The
catalog does. Anyone reasoning about "where do projects come from" who greps for `controlplane`
lands on a read-only k8s view of an object type that does not exist, and the FGA model's `type
project` has nothing to do with it. `HANDOFF-lakehouse.md:130` already flags "the two `/api`
meanings" as an unreconciled estate-wide decision.

### 3.5 — P5 · The ClusterRole is broader than the code needs · **Severity: Low**

`controlplane.yaml:94-96` grants `get,list,watch` on `networking.k8s.io/ingresses` **cluster-wide**.
The code's only use is `list_namespaced_ingress(namespace, label_selector="platform.rask.io/project")`
(`k8s.py:39`) — namespaced and label-selected. Cluster-wide `list` on ingresses discloses every
hostname and TLS secret reference in the cluster to anything that can read that SA's token.
Cross-namespace access does require a ClusterRole, so this is not removable as such, but it is
wider than the call. Moot if the service is deleted.

### 3.6 — the pre-existing performance finding, for completeness

`open_python-audit.findings.json:3061` already records that `GET /api/projects/` costs `1 + N`
serialized blocking k8s calls (one `ingress_host` per namespace with an identical selector), so
latency grows linearly with project count and one slow namespace stalls the whole list. Also moot if
the service is deleted; noted so nobody re-files it.

### 3.7 · Recommendation

**Delete it, rather than gating it.** The service, the gateway row, the chart template (Deployment,
Service, SA, ClusterRole, ClusterRoleBinding), the `controlplane.*` values block,
`frontend/packages/api/src/projects.ts`, and `home.remote.ts`'s `getProjects`. It has no producer,
no consumer, has been returning 503 in production since at least the handoff that recorded it, and
its name misleads on the estate's most privileged plane. Adding an auth dependency would preserve
all four problems and fix only the latent one.

Keep the `/api/projects` **path** free afterwards rather than reassigning it — the gateway's
no-catch-all rule means an unmatched `/api/*` 404s (`gateway/__init__.py:133-135`), which is the
honest answer for a retired surface.

Counter-argument worth hearing: if an operator for `platform.rask.io` exists in another repo (the
estate has out-of-repo infra precedent — CLAUDE.md says the remote KubeRay cluster is "managed
elsewhere"), then this is a half-finished integration, not dead code, and the right move is to
finish it with a door on it. **I did not check other repos** — see §7.

---

## 4 · The tenancy authorization model: one real gap and three fragilities

### 4.1 — P6 · A project admin cannot add anyone to their own project · **Severity: High**

This is the finding I would attack first, because it is a live product gap, not a latent one.

`type project` (`model.fga:55-66`) has three assignable relations: `team: [team]`,
`admin: [user, role#assignee]`, `member: [user, team#member, role#assignee]`.

Every write of those tuples in the entire estate:

1. `seed_project_admin` (`fga_deps.py:626`) — the creator's own `admin`, once, at create time.
2. `POST /v1/access/tuples` (`access_admin.py:291`) — the raw tuple editor.

That is the complete list. The catalog's project router has four routes and none of them touches
membership: `POST ""`, `GET ""`, `GET "/{id}"`, `DELETE "/{id}"` (`projects.py:174,235,257,280`).

And the raw tuple editor is **estate-admin gated**: every `access_admin` route funnels through
`_estate_gate` (`access_admin.py:145`), which is
`require_relation(..., "can_observe_events", settings.fga_root_object)` (`:158`).

The per-object grant surface cannot substitute, and the reason is precise:
`_GRANTABLE_BASE = ("owner", "writer", "reader", "validator")` (`access.py:61`), and
`_grantable_relations` intersects that with what the type defines (`:65-70`). `type project` defines
none of those four. So on a `project:` object the grantable set is **empty** — the endpoint would
reject any relation a caller named.

**Consequence.** Adding a second admin, adding a member, or attaching a team to a tenant is an
estate-admin operation. The person who created a project, and holds `admin` on it, cannot invite
anyone into it. The FGA model has `member` and `team` rungs and a whole cascade built on them, and
the only UI that can write them is the raw tuple editor at `/settings/access`
(`home/src/lib/remote/access.remote.ts:106`) behind the estate bar.

Worth stressing that the model itself is fine — `can_administer: admin` (`model.fga:61`) is exactly
the right door for this. **There is no endpoint behind it.** This is a missing API, not a missing
relation.

**What I think should exist:** `POST /v1/projects/{id}/members` and its DELETE, gated on
`project:<id>#can_administer`, restricted to writing `member`/`admin` on that one project object,
audited like every other grant. Deliberately *not* a generalization of `access.py`'s grant route —
that one is keyed on the four data-plane rungs and should stay that way.

Open question for whoever takes it: should a project admin be able to grant `admin` (making the
tenant self-governing) or only `member` (keeping admin-minting at the estate)? I lean
**admin-grantable**, because a tenant whose only admin leaves is otherwise unrecoverable without
estate intervention, and because `delete` already trusts `can_administer` with the whole tenant
(§4.2). But it is a product decision and it is not written down.

### 4.2 — P7 · Create and delete are gated at different levels · **Severity: Low, needs a written ruling**

- create → `can_observe_events` on the **estate root** (owner-tier, estate-wide)
- delete → `can_administer` on the **project itself** (tenant-tier)

So a project admin can retire a tenant they had no authority to mint. I think this is *defensible*:
delete is hedged by deletion protection and bottom-up emptiness (it refuses while warehouses exist
and names them, `projects.py:296-298`), so it is not a destructive primitive; and asymmetry in the
other direction — estate-admin to delete — would make every tenant retirement a platform ticket.

But the asymmetry is not stated anywhere, and an auditor walking the model will read it as a bug.
It needs one line in the module docstring saying it is deliberate and why.

### 4.3 — P8 · `can_observe_events` is the de-facto estate-admin bar under a name that says "read the feed" · **Severity: Medium (audit legibility)**

The relation is named and documented for one job — "estate-wide control-plane event feed (GET
/v1/events)" (`model.fga:87-89`). It is used as the gate for at least four unrelated privileges:

| Gated operation | Site |
| --- | --- |
| read the estate event feed | its documented purpose |
| **mint a tenant** | `projects.py:192` |
| read/write/delete **any tuple in the estate** | `access_admin.py:158` |
| register an object store (`POST /v1/stores`) | `model.fga:96-98` |

Someone reviewing an access list and seeing `user:x owner warehouse:lance_catalog` has to already
know all four to understand what they just approved. The name understates it by a wide margin. A
grant that confers arbitrary tuple-writing across the estate should not be readable as "can watch
the activity feed".

I would not repoint the existing checks in a hurry — that is a live authz change with a reseed
implication. But the model comment at `:87` should enumerate every consumer, at minimum, and the
honest fix is a distinctly-named `can_administer_estate` that the others alias to.

### 4.4 — P9 · The estate root is a `warehouse:` object, and root-ness is a convention held in code · **Severity: Low, fragile**

`fga_root_object` defaults to `warehouse:lance_catalog` (`config.py:225`). There is no `estate` type.
So `can_observe_events` and `can_browse_storage` are declared on `type warehouse` (`model.fga:90`,
`:106`) and therefore exist on **every warehouse instance**, where they resolve to that warehouse's
`owner`.

Nothing is broken today: the app checks both relations only against `settings.fga_root_object`, and
the model comments say so explicitly (`:88`, `:93-94`). But the guarantee lives in call sites, not
in the model. Any future code path that checks `can_observe_events` on a non-root warehouse gives
every warehouse owner an estate-wide privilege, and it will typecheck, pass `fga model test`, and
read as correct.

The model already documents the reason a dedicated type was rejected for `store`
(`model.fga:100-105`: nothing would write the tuples for the four code-defined default stores, so
the gate would deny everyone including the estate owner). That reasoning is sound for `store`. It
does **not** obviously apply to an `estate` type, which would need exactly one seeded tuple. Worth
re-examining.

### 4.5 · Checked and found sound — do not re-file these

Recorded so the next auditor does not spend the afternoon I did:

- **`fga_enabled` without `oidc_enabled` is refused at boot** (`config.py:318-319`). This matters,
  because `require_relation` no-ops when `token is None` (`fga_deps.py:802`) — the gate would be
  silently off. With OIDC on, a missing bearer 401s before any handler runs
  (`security.py:142-144`), so `token is None` reaches the gates only on an auth-off stack, which is
  the dev posture and is intended.
- **The per-object grant surface cannot escalate into a project** — `_GRANTABLE_BASE` excludes
  `admin`/`member`/`team` (`access.py:61`). (This is also what causes P6; it is correct as a guard
  and wrong as the only path.)
- **`access_admin` validates every type/relation against the compiled model** and refuses
  non-assignable derived `can_*` writes with a 400 rather than letting OpenFGA 400 fail closed to a
  503 (`access_admin.py:74-96`, `:233-235`).
- **Create is genuinely idempotent** and carries `protected` forward (`projects.py:196-203`).
- **The id regex is `\Z`-anchored** and single-sourced (`identifiers.py:31-33`).
- **A team cannot self-escalate into a tenant.** `project#admin` includes `member from team`
  (`model.fga:58`), so writing `project#team` would make every team member an admin — but that
  tuple is only writable through the estate-gated tuple editor. The escalation path exists in the
  model and is closed at the API.

---

## 5 · Why `transaction` has no create door (and the trap in the reminder)

This is the coverage-table line added to `model.fga:35-39` on 2026-08-08, spelled out because it
reads cryptically.

**The doctrine.** For every parent→child pair, the parent should carry `can_create_<child>` — at
creation time the child does not exist, so there is nothing else to check against. `project` has
`can_create_warehouse` / `can_create_annotation_project` (`model.fga:62,66`); `warehouse` and
`namespace` each have `can_create_namespace` / `_table` / `_materialized_view` (`:80-86`).

**Why `transaction` is exempt: there is no creation request to gate.** The entire transaction
surface is `endpoints/transactions.py`, 33 lines, two routes:

```python
@router.post("/{id}/describe")   # :21 — both take an id that ALREADY exists
@router.post("/{id}/alter")      # :28
```

There is no `POST /v1/transaction`. Transaction ids come *out* of other operations — insert, merge,
index build and column ops all return a `transaction_id` (`tables.py:714`, `data.py:396`,
`indices.py:5`, `columns.py:210`) — and those operations were authorized against their table or
namespace before anything was minted. The creation moment is inside a check that already ran. A
transaction is closer to a receipt than to a resource you request. "Enforced outside OpenFGA by
construction" means exactly that: no code path produces one without a prior gate.

**The trap the reminder exists for.** `type transaction` declares `define parent: [namespace,
warehouse]` (`model.fga:176`) — **two** allowed parent types, where nearly every other child accepts
one. A future `can_create_transaction` must therefore be defined on `namespace` *and* on
`warehouse`, and the endpoint must check whichever the caller named. Adding it to `namespace` alone
looks complete, passes review, and leaves warehouse-rooted creates entirely ungated.

This is not hypothetical pedantry — the same two-parent hazard is already documented one type up:
`model.fga:82-85` records that `can_create_materialized_view` had to exist on **both** `namespace`
and `warehouse`, because when `fga_lock_root_create` is on the app checks the parent relation
against the warehouse root, and a missing relation is an OpenFGA 400 that fails closed to a **503
for every caller, owners included**. Same shape, already paid for once.

**Related, and worth its own look:** transactions are authorized by `_authorize_transaction`
(`fga_deps.py:284-318`), which has two branches — a namespaced id `<ns>$<txn>` resolves
parent-scoped against `namespace:<ns>` (`can_get_metadata` / `can_update_properties`), and an opaque
root id resolves object-scoped against `transaction:<id>` (`can_describe` / `can_set_status`). The
docstring argues the two branches gate on the same privilege, since `can_set_status: committer` and
`committer ⊇ writer from parent`. That reasoning looks right to me but I did not test it, and
`tests/unit/test_fga_model_contract.py` proves only that every (type, relation) pair exists — not
that the branches are equivalent. **UNVERIFIED.**

---

## 6 · The two dead `transaction` permissions

`type transaction` declares four actions (`model.fga:190-193`):

```
define can_describe: viewer
define can_set_property: editor      # dead
define can_set_status: committer
define can_cancel: committer         # dead
```

**"Dead" means referenced by nothing** — no other relation names them in its expression, and no code
path passes them to a Check. Established by reading `_authorize_transaction`, the only place
transaction ops are authorized, which picks between exactly two:

```python
relation = "can_describe" if is_read else "can_set_status"   # fga_deps.py:316
```

Two endpoints, two relations. `can_set_property` and `can_cancel` are never the relation in any
Check this estate can make, on either branch.

**Why they were left in place** (recorded at `model.fga:182-189`): they are leaf permissions, so
nothing inherits *through* them and they cost only clarity; the openfga skill's optimize-simplify
rule says a usage sweep produces removal *candidates*, never removals, because a relation dead in
code can still be live in seeded tuples or an operator runbook; and a model change must ship with
`fga model test` green, which needs the `fga` CLI — **not installable in this sandbox**, the GitHub
release binary 403s through the proxy. Deleting them without that gate would be the half-done change
the repo's principles forbid.

**The live consequence, which is the part actually worth deciding.** `alter_transaction` takes an
`AlterTransactionRequest` carrying a set of state actions, and gates the whole thing at one
writer-tier check. So **anyone who can commit a transaction can also edit its properties** — the
model draws a finer line (`editor` may set properties but not commit; `committer` may commit) that
the endpoint does not use. Either the endpoint should authorize per-action and use these two doors,
or the model should stop claiming a distinction nothing enforces. Right now it claims one.

**If the decision is to delete, it is not a two-line removal.** `can_set_property` is the only
*action* `editor` backs — but `editor` is not itself dead: `viewer` inherits from it
(`model.fga:180`) and `editor` carries its own direct-grant slot (`:179`). Removing the permission
is one line; removing the rung underneath changes the inheritance chain and drops a grant tier.
Two decisions, not one.

---

## 7 · What I did NOT verify

Attack these first — they are where I am most likely wrong.

1. **Whether a `platform.rask.io` operator exists outside this repo.** I searched this repo only.
   If one exists, P2 and §3.7 change from "delete" to "finish and gate". This single fact decides
   §3 entirely.
2. **The actual status code behind the 503.** RBAC 403 and missing-CRD 404 are indistinguishable
   through `_K8S_ERRORS`. One `kubectl get crd`, or one uvicorn log line, settles P2 and corrects
   (or vindicates) `HANDOFF-lakehouse.md:107`.
3. **Whether `/api/projects` is anonymously reachable at a real ingress.** The chain is read from
   templates; I had no cluster. An ingress-level auth annotation or an intermediate proxy I did not
   find would defeat P1.
4. **Whether anything outside `frontend/` calls `listProjects`.** I grepped `frontend` excluding
   `node_modules`. The `tests/e2e` standalone Playwright project has its own lockfile and I did not
   sweep it. (The one lakehouse spec that mentions the path,
   `lakehouse/e2e/admin/auth.spec.ts:40`, hits `/lakehouse/api/projects` — a zone-relative BFF path,
   asserting only "API routes do not redirect". It does not exercise the controlplane and does not
   block deletion.)
5. **P6's blast radius.** I established that no *catalog* endpoint writes project membership. I did
   not check whether some other service (annotator? lineage?) writes `project#member` tuples
   directly through `service_kit.governed.fga`. If one does, P6 narrows.
6. **Whether the estate currently has more than one project admin anywhere.** If it does, someone
   found a path I did not, and P6 is wrong.
7. **§5's claim that the two `_authorize_transaction` branches gate on equivalent privilege.** Read
   from the docstring's argument, not tested.
8. **Everything in §6 requiring `fga model test`.** No CLI in this sandbox.

---

## 8 · Recommended order

1. **Settle §7.1** (does the operator exist). One question to whoever owns the platform CRDs. Every
   §3 decision hangs on it, and it costs a message.
2. **P6 — project membership API.** The only finding that blocks real users today. Needs the
   admin-vs-member product ruling in §4.1 first.
3. **P1+P2+P3 — delete the controlplane**, assuming §7.1 comes back "no operator". One commit:
   service, gateway row, chart template, values block, `@rask/api/projects.ts`, `getProjects`. Keep
   `/api/projects` unassigned.
4. **P8 — enumerate `can_observe_events`'s consumers in the model comment.** Comment-only, no
   semantic change, immediately reduces the audit hazard. The rename to `can_administer_estate` is a
   separate, larger change.
5. **P7 — one line in `projects.py`'s docstring** stating the create/delete asymmetry is deliberate.
6. **§6 — decide whether `alter_transaction` authorizes per-action.** If yes, the two "dead"
   permissions become live and the question closes. If no, delete them (with `fga model test`) and
   drop the model's claim to a distinction nothing enforces.
7. **P9 — re-examine an `estate` type.** Lowest urgency; genuinely fragile; the `store`-type
   argument that rejected a dedicated type does not transfer.
