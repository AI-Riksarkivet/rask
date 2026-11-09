# Lakekeeper's control plane and security, read for what rask can learn

> Research note. Lakekeeper is the reference, never the stack: every proposal below is stated on rask's own stack
> (Lance and lance-namespace, OpenFGA, Dex, Kubernetes ServiceAccounts, Dapr on NATS JetStream, Helm under Argo CD), and
> none adopts Lakekeeper's code, Iceberg, or a Postgres catalog database. Every Lakekeeper claim cites
> `github.com/lakekeeper/lakekeeper` at `main` `e9b202d1f04c579fdcdc2749ac1fe74b0ab8ca41` (2026-11-09); every rask claim
> cites this repository at `c74060dc3`. Paths without a prefix under "Lakekeeper" are relative to that checkout; rask
> paths are relative to this repository. Read 2026-11-09. Nothing was run or measured: every rask claim is from reading
> the code, and the one defect named (finding 1) has not been reproduced live.

**Status, 2026-11-09 (the owner's decisions).** Finding 1 is admitted as a counted row, LH-568 #788, to be fixed before
XC-081 #203. Findings 2 and 3 are parked as LH-569 #789 and LH-570 #790. Findings 4 to 7 are not taken up.

Scope: `crates/authz-openfga` in full, authentication in `crates/lakekeeper/src/service/authn.rs`, credential vending in
`crates/lakekeeper/src/service/storage`, secrets (`crates/lakekeeper-secrets-kv2`, `service/secrets.rs`), the hierarchy
and its lifecycle in `api/management/v1`, the audit log (`crates/audit-macros`, `service/events/backends/audit`), and the
request seams (admission, maintenance mode, idempotency, pagination, caching).

The overall reading: rask already ported most of what this part of Lakekeeper does, often further (time-boxed grants
through `non_expired_grant`, a layout-aware session policy, a content-addressed model, `HIGHER_CONSISTENCY` on every
read, a scope probe at warehouse create). What remains is a short list of places where the port stopped early.

## 1. Summary, ranked by value to rask

1. **A rename moves a table across a managed-access boundary without the destination's grant authority.** Ask
   `manage_grants` on both sides of a cross-namespace rename when either side is under managed access. Zero trust; small.
2. **A warehouse (and project) declared in git has no lock against API drift, and the operator cannot create a
   project.** Add a `managed_by` marker that refuses spec mutations except from the git-declared operator, and land
   LH-076 so `POST /management/v1/projects` checks `can_create_project`. GitOps; medium.
3. **The privilege ladder lacks `describe` (see without reading data) and `modify` (DDL and lifecycle without grant
   authority).** Add the two rungs, so discovery does not need data access and a pipeline that drops or restores does
   not also hold grant authority. Zero trust; medium, a model change.
4. **A fresh project or warehouse create does not check that its id is free of tuples.** Refuse a first create of a
   caller-chosen id that any tuple still names. Zero trust; small.
5. **A listing asks OpenFGA once per item even when one grant on the parent answers every item.** Check the parent
   first and skip per-item checks when it covers them. Performance; small.
6. **Vended credentials are minted per request with no cache.** If a platform STS rate-limits, add a per-subject,
   single-flight cache served for half the credential's life. Cloud native; small, only when measured to matter.
7. **Audit record fields are free-form beyond the fixed core.** Pin each action's field set in a schema the suite
   checks emitted records against. Low.

## 2. Findings

### 2.1 A rename moves a table across a managed-access boundary without the destination's grant authority

**What Lakekeeper does.** Moving a table between namespaces is two actions. On the source, `can_move: manage_grants and
modify_effective` (`authz/openfga/v4.15/components/lakekeeper_table.fga:41`): the mover must hold grant authority on the
table, because its grants travel with it. On the destination, `can_accept_moved_tabular: (create_effective but not
managed_access_inheritance) or (manage_grants and create_effective)` (`authz/openfga/v4.15/components/namespace.fga:46`,
and the namespace twin at `:45`): into a namespace under managed access, create rights are not enough, because "A moved
object arrives with its grants. Under managed access, grants here need grant authority, so accepting one does too"
(`namespace.fga:43-44`).

**What rask does.** The rename door (`services/catalog/src/catalog/api/v1/endpoints/tables.py:1540-1561`) asks
`can_drop` (owner) on the source (`services/catalog/src/catalog/api/fga_deps.py:170`), `can_create_table` (writer) on
the destination parent (`fga_deps.require_create_on_parent`, `fga_deps.py:2478-2493`), and refuses a cross-tenant role
grant following the table (`fga_deps.require_grants_may_follow`, `fga_deps.py:2120-2135`, which returns at once when the
destination shares the source's top-level namespace). Managed access is not consulted. rask's model gives managed
access the same meaning Lakekeeper's does: `manage_grants: … or (owner but not managed_access_inheritance from parent)`
on namespace and table (`packages/lakehouse/src/lakehouse/governed/auth/model.fga:461`, `:547`).

**The gap.** Two paths escape managed access, both inside one tenant:

- *Into* a managed namespace: an owner in an unmanaged namespace grants `reader` on table T to someone, then renames T
  into a managed namespace where it holds only `writer`. The grant arrives with T (grants are held by the uuid, ADR 0173),
  and the managed namespace's grant authority never approved it.
- *Out of* a managed namespace: an owner of T inside a managed namespace holds no `manage_grants` on T there. Renaming T
  into an unmanaged namespace where it can create gives it `manage_grants` on T again.

**Proposal.** In the rename door, when the source and destination parents differ, require `manage_grants` on the table
when the source is under managed access, and `manage_grants` on the destination parent when the destination is under
managed access. Expressed in the model as two actions so the model, not the app, owns the mapping: `can_move` on table
(`manage_grants and can_drop`) and `can_accept_moved_table` on namespace and warehouse (`(writer but not
managed_access_inheritance) or (manage_grants and writer)`), each asserted in `model.fga.yaml`. The door then asks
`can_move` on the source and `can_accept_moved_table` on the destination instead of `can_drop` and `can_create_table`
alone. A same-namespace rename (a new name, same parent) keeps today's rule.

**Four facts.** Zero trust: met, closes a path that grants access no grant authority approved, which makes it
pre-approved work under the owner's rules. GitOps: neutral (a model change ships in the image and the `openfga-model`
hook writes it). Cloud native: neutral. Lance-idiomatic: neutral; `RenameTable` with `new_namespace_id` is the spec's own
door and stays so.

**Effort and risk.** Small: two model relations, their assertions, one door change, one integration test of the two
escape paths. Risk: a caller who renames across namespaces today without grant authority under managed access is
refused, which is the intent.

### 2.2 A git-declared warehouse has no lock against API drift, and the operator cannot create a project

**What Lakekeeper does.** A warehouse carries `managed_by` (`crates/lakekeeper/src/service/catalog_store/warehouse.rs:90-111`):
`self-managed` by default, or managed by the instance, in which case every spec mutation needs instance-admin privilege
whatever the caller's grants. The locked set is explicit (`crates/lakekeeper/src/service/authz/mod.rs:1240-1270`):
delete, storage and storage-credential updates, activate and deactivate, rename, soft-deletion settings, protection;
data-plane and listing actions are not locked. Creating a managed warehouse requires instance-admin privilege and a
refusal is audited as an authorization failure (`crates/lakekeeper/src/api/management/v1/warehouse/mod.rs:162-167`,
`:590-598`). Setting the marker is an instance-admin-only action that is "never represented in OpenFGA"
(`crates/lakekeeper/src/service/authz/instance_admin.rs:1-60`). Instance admins are declared in deployment config,
cover control-plane actions only, never `ReadData`/`WriteData`, and "cannot be revoked from within Lakekeeper at runtime;
change the deployment config to add or remove admins" (`crates/lakekeeper/src/config.rs:792-811`). The model's
`server#operator` is "designed to be used by machines that provision resources inside Lakekeeper, i.e. a
`KubernetesOperator`" (`authz/openfga/v4.15/components/server.fga:15-18`).

**What rask does.** Projects and warehouses are created through doors (`services/catalog/src/catalog/api/v1/endpoints/projects.py:217`,
`warehouses.py:164`), idempotently, so a GitOps re-POST converges (`services/catalog/src/catalog/services/warehouses.py:193-268`
names that case). The same doors let any `owner` of a warehouse deactivate it, protect or unprotect it, change its grace
or delete it (`warehouses.py:578`, `:601`, `:665`, `:1153`), and nothing marks a warehouse as one git declares. The model
already has the machine identity: `estate#operator` and `can_create_project: admin or operator` (`model.fga:138`, `:148`),
and ADR 0143 decides the create door checks `can_create_project`. The door still checks `can_observe_events`
(`projects.py:235`), and the model's own comment says so (`model.fga:31-33`); the row is LH-076 (#128, open). So an
operator identity cannot create a project today, and the planned rask operator has no seam.

**The gap.** A warehouse that git declares can be changed through the API by its owner, and the change survives until
someone notices: the deactivate-versus-re-POST race that `warehouses.py:193-268` handles is exactly a live-only state
that the next reconcile does not undo. The operator path that would carry git's declaration is blocked at the project
door.

**Proposal.** (a) Land LH-076, so the operator can create a project. (b) Add `managed_by` to the project and warehouse
records (`self` by default, `operator` for a record the operator wrote). A record with `managed_by: operator` refuses
every spec mutation (deactivate, activate, protection, grace, delete, rename) unless the caller is the operator principal
named in the chart's values (one `PrincipalKey`, rendered from git, read by the catalog at boot), and the refusal is
audited as a denial. Grants, namespaces and tables below the warehouse stay under the warehouse's own grant-holders, as
Lakekeeper leaves data-plane actions unlocked. The marker is set only by the operator principal at create, never
granted through OpenFGA, so no tuple can lift the lock. Whether a git-declared project or warehouse is created by the
planned operator or by a reconciler Job is the owner's choice; the marker serves either.

**Four facts.** GitOps: met, a declared object can no longer drift through its own doors, and the authority that may
change it is itself in git. Zero trust: met, the operator is an identity proved by its ServiceAccount token, not by
position; the lock narrows what an owner may do. Cloud native: met, it is the seam a Kubernetes operator reconciles
through. Lance-idiomatic: neutral, the project and warehouse are rask's levels above the spec.

**Effort and risk.** Medium. LH-076 is a door repoint. The marker is a record field, one guard shared by six doors, a
setting, and an integration test that an owner's deactivate of an operator-managed warehouse is refused and the
operator's is admitted. Risk: an estate whose warehouses were created by hand stays `self` and is unaffected.

### 2.3 The privilege ladder lacks `describe` and `modify`

**What Lakekeeper does.** Five assignable privileges per object, each with a bare relation (what was granted, listed
from) and an `_effective` twin (what it implies, plus what flows from the parent): `describe < select < write < modify`,
with `create` beside them on containers (`authz/openfga/v4.15/components/lakekeeper_table.fga:15-24`,
`namespace.fga:19-28`, `warehouse.fga:21-30`). `describe` gives metadata and list visibility without data
(`can_get_metadata: describe_effective`, `can_include_in_list: can_get_metadata`, `lakekeeper_table.fga:37`, `:42`).
`modify` gives drop, undrop, rename and protection (`lakekeeper_table.fga:33-44`) without grant authority, which stays
on `manage_grants` and ownership (`:13`). `create` on a namespace creates children without reading or writing the
children others made (`namespace.fga:23-24`, `:39-42`, and `:50`: "`create` shows the containers it may create in, not
the tables … inside").

**What rask does.** Three concentric rungs, `owner > writer > reader`, plus the side rungs validator, maintainer,
classifier and publisher (`model.fga:235-273`, `:430-450`, `:529-542`). Metadata needs `reader`, which is data access:
`can_get_metadata: reader` on table (`model.fga:552`) and `can_read_data: reader` (`:560`); table listings filter on
`can_read_data` (`services/catalog/src/catalog/api/v1/endpoints/namespaces.py:1299-1357`). Drop, protection, restore,
rename, branch and version deletion, retention policy and access listing all resolve to `owner`
(`model.fga:579`, `:597`, `:615`; `fga_deps.py:153-215`), and `owner` carries grant authority outside managed access
(`model.fga:547`). Creating a table needs `writer` (`model.fga:485`), which also writes every table in the namespace.
ADR 0070 recorded these devices as what the port was for; the `describe` and `modify` rungs were not carried over.

**The gap.** Two least-privilege cases have no rung. A data steward, a lineage reviewer or a discovery UI cannot see
that a table exists, its schema or its versions without holding read on its rows. A pipeline that replaces, restores or
retires tables must hold `owner`, and outside managed access `owner` can grant the table to anyone.

**Proposal.** Add `describe` and `modify` as assignable rungs on warehouse, namespace and table, concentric with the
existing ones (`reader` implies `describe`; `owner` implies `modify`), each inheriting from the parent as the others do.
Repoint `can_get_metadata` and listing visibility to `describe`, and keep `can_read_data`, the vend and every data door
on `reader`. Repoint the lifecycle actions that destroy or relocate but grant nothing (`can_drop`, `can_set_protection`,
`can_restore`, `can_create_branch`, the version and tag deletes) to `modify`, and leave `can_grant_*`, the access
listing and `manage_grants` on `owner`. Whether `create` is split from `writer` is a separate question with a weaker case
in rask, since the medallion writer usually writes what it creates; it is left out of this proposal. Pre-production, so
no tuple migration: existing `owner` and `reader` grants keep their meaning because the new rungs are implied by them.

**Four facts.** Zero trust: met, each grant gives less than today for the two cases. GitOps: neutral (model in the
image). Cloud native: neutral. Lance-idiomatic: met, describe is exactly `DescribeTable` without `vend_credentials`,
and the spec separates metadata from data access in the same way (`lance_docs/ns_catalog/spec.yaml`, `DescribeTableResponse.storage_options`).

**Effort and risk.** Medium: model, `model.fga.yaml` assertions per rung, the door-to-relation map in `fga_deps.py`,
the grant doors' vocabulary, the console's grant picker, and the listing filter. Risk: a door mapped to the wrong
rung; the model's assertions and the existing integration tests over the doors are the guard.

### 2.4 A fresh create of a caller-chosen id does not check that the id is free of tuples

**What Lakekeeper does.** Every create first runs `require_no_relations` (`crates/authz-openfga/src/authorizer.rs:1589-1667`),
called from `create_project`, `create_warehouse`, `create_role`, `create_namespace` and the tabular creates
(`authorizer.rs:846-891`, `:800`, `:910`, `:1000`): it refuses 409 when any tuple names the new object, as object or as
user, so a new object cannot inherit grants or edges a deleted one left. The refusal names no conflicting tuple to the
caller and logs them instead, because the id is caller-chosen (`authorizer.rs:1638-1647`).

**What rask does.** Tables, namespaces and roles are keyed by a minted uuid (ADR 0173; `services/catalog/src/catalog/services/roles.py:7-13`),
so they cannot meet an old object's tuples. Projects and warehouses are keyed by a caller-chosen id (`projects.py:97`,
`warehouses.py:124`). Their deletes revoke tuples first and the record second (`projects.py:431-520`; the warehouse
delete at `warehouses.py:1153`), and maintenance's drift reconciler revokes the grants of an object no registry names
(`services/catalog/src/catalog/services/grant_reconcile.py:1-60`). The create door seeds the admin tuple without reading
what already names the id (`projects.py:236-258`).

**The gap.** A delete whose revoke wrote part of its tuples and then lost OpenFGA, or a tuple written by hand, leaves
grants on `project:<id>` until the next reconcile. A project created again under that id in the window inherits them.

**Proposal.** On the path where no registry record exists (`existing is None`, `projects.py:247`, and the warehouse
twin), read the store for any tuple naming the object, and for any tuple naming it as user, before writing the record;
refuse 409 with a message that names no tuple, and log what was found. The idempotent re-POST of an existing record is
unaffected, so a GitOps re-apply still converges.

**Four facts.** Zero trust: met. GitOps: met, re-applies are untouched. Cloud native and Lance-idiomatic: neutral.

**Effort and risk.** Small: two reads (`fga.read_object_tuples` exists) in two doors and one integration test. Risk:
an operator recreating a project after a half-failed delete is refused until the reconciler or a re-run delete clears
it, which is the intent.

### 2.5 A listing asks once per item when one grant on the parent answers every item

**What Lakekeeper does.** Before filtering a page it checks `can_list_everything` on the parent
(`authz/openfga/v4.15/components/namespace.fga:56`, `warehouse.fga:58`), and when that holds it skips per-item checks
(`crates/lakekeeper/src/server/namespace.rs:104-152`, `server/tabular.rs:414-422`). Otherwise it reads a page from the
backend, filters it with a batch check, and refills from the next backend page until the page is full
(`crates/lakekeeper/src/server/mod.rs:251-317`).

**What rask does.** `list_tables` drains the native listing unpaginated (`namespaces.py:253`, `:1338`) and judges every
candidate past the cursor in batches that double up to `LISTING_BATCH` = 100 (`fga_deps.py:1564`, `judged_page` at
`:1597-1640`), resolving each name to its uuid first. A caller holding `reader` on the namespace holds it on every table
below (`model.fga:531`), yet each table is still resolved and checked.

**Proposal.** In `authorized_page`, check the relation once on the parent object; when it holds, every child is allowed,
so skip the per-item resolution and batch checks and page the names directly. The parent check uses the same rung
(`reader` on the namespace for `can_read_data` on its tables, or `describe` after finding 2.3). Draining the native
listing stays, since the keyset cursor needs a sorted, deduplicated list.

**Four facts.** All neutral; zero trust is unchanged because the parent relation implies the child's by the model.

**Effort and risk.** Small. Risk: none if the parent relation is one the model makes imply every child's; a
`model.fga.yaml` assertion pins that implication.

### 2.6 Vended credentials are minted per request with no cache

**What Lakekeeper does.** Temporary credentials are cached under a key of (table location, permission, warehouse,
table, a hash of the storage profile and of its credential) with single-flight loading
(`crates/lakekeeper/src/service/storage/cache.rs:57-75`, `service/storage/s3.rs:701-725`,
`service/storage/mod.rs:235-240`) and served for half their remaining life, capped at an hour (`cache.rs:93-114`). The key
holds no caller, so two callers of one table share a credential.

**What rask does.** Every vend calls `AssumeRoleWithWebIdentity` (`services/catalog/src/catalog/core/vending.py:1129-1153`)
with the caller's subject as the session name (`vending.py:341-349`), so the store's log names the person or service the
session was vended for, and the policy is shaped by the table's layout and tier.

**The gap.** None today on the development store. On a platform STS with a request quota, a burst of describes of one
table is one STS call each.

**Proposal.** Only when a platform STS is measured to throttle: a per-replica single-flight cache keyed by (subject,
table uuid, tier, branch, the canonical policy text), served until half of `expires_at_millis` remains. Keeping the
subject in the key keeps rask's per-caller attribution, which Lakekeeper's shared key gives up.

**Four facts.** Zero trust: met only with the subject in the key; a shared key would break attribution. Cloud native:
met (protects the platform's STS). GitOps and Lance-idiomatic: neutral (`expires_at_millis` is the spec's refresh
signal).

**Effort and risk.** Small; risk is a revoked caller using a cached session for at most half its lifetime, the same
window a session already handed out has.

### 2.7 Audit record fields are free-form beyond the fixed core

**What Lakekeeper does.** Audit records have three shapes (`authorization`, `replay`, `operation`) and a versioned
format (`crates/lakekeeper/src/service/events/backends/audit/mod.rs:46-120`); every type that reaches the log carries
`#[audit_part]` (`crates/audit-macros/src/lib.rs:1-80`), which fixes the wire field and its closed vocabulary, and the
suite validates emitted records against a schema with fixtures (`service/events/backends/audit/validate.rs`,
`audit/fixtures/`).

**What rask does.** A closed action and outcome vocabulary and a format version (`packages/lakehouse/src/lakehouse/governed/audit.py:27-87`),
with the remaining fields passed as `**fields` (`audit.py:136-167`), so a call site can add, drop or rename a field
without the format changing.

**Proposal.** Give each `AuditAction` a Pydantic model of its extra fields and have `audit()` validate against it, so a
renamed field fails where it is written, and bump `AUDIT_FORMAT` with the models. No new test file: the doors' existing
integration tests already emit records.

**Four facts.** All neutral; it strengthens the compliance trail zero trust relies on. **Effort and risk.** Small to
medium (one model per action); low risk.

## 3. Looked at, nothing to take

- **Model versioning and migration** (`crates/authz-openfga/src/migration.rs:17-111`, `:225-254`; `authz/openfga/v4.0`,
  `v4.15`). Lakekeeper names a model by version tuples and checks at boot that the model holds every relation the server
  uses. rask finds its model by canonical body in the store's history (`packages/lakehouse/src/lakehouse/governed/auth/write_model.py:64-105`),
  which also catches a rule narrowed under an unchanged name; that is stronger. Tuple migrations between versions do
  not apply to a pre-production estate.
- **Check consistency.** Lakekeeper checks at default consistency and uses `HIGHER_CONSISTENCY` only for listings and
  writes (`crates/authz-openfga/src/authorizer.rs:63-75`, `:1249-1256`, `:1455-1466`). rask asks every read at
  `HIGHER_CONSISTENCY` so a revoke is answered by the next request (`packages/lakehouse/src/lakehouse/governed/fga.py:1075-1086`).
  rask's is the better default.
- **Batch-check chunking** (`authorizer.rs:1513-1563`, `max_batch_check_size`). The Python SDK rask uses chunks a batch
  at `max_batch_size` 50 itself (`openfga_sdk/client/client.py:782-832` in the installed SDK).
- **Hierarchy tuple reconcile** (`crates/authz-openfga/src/reconcile.rs:1-60`). rask has the equivalent, run from
  maintenance's drift report and judged on the catalog's registries (`services/catalog/src/catalog/services/grant_reconcile.py`).
- **Grants API, pass-grants delegation, revoke as administration, managed access, role nesting, tags with
  per-tag `apply`** (`crates/authz-openfga/src/grant.rs:1-19`; the v4.15 components). Ported (ADR 0070); rask's
  `classification` type with `curator`/`apply` is the tag counterpart (`model.fga:654-700`).
- **Project lock-out protection** (`project.fga:10-13`). rask guards the last admin in its member and role doors.
- **OIDC verification** (`crates/lakekeeper/src/service/authn.rs:116-305`, `:476-689`). Multi-provider routing, required
  claim rules and a refusal to boot with every provider down. rask verifies one Dex issuer with an asymmetric algorithm
  allowlist, audience per door, split-horizon discovery and a paced unknown-`kid` refetch (`packages/lakehouse/src/lakehouse/governed/oidc.py:1-41`),
  and keys principals as `<idp-id>~<subject>` exactly as Lakekeeper does (`principal.py:1-114`, `authn.rs:37`, `:80`).
  Required-claim rules answer multi-IdP tenancy that Dex's connector mapping already answers in rask (`idp_identity.py`).
- **Kubernetes ServiceAccount authentication** (`authn.rs:318-375`; `config.rs:1175-1196`). Lakekeeper uses TokenReview
  with `uid` as default subject. rask verifies offline with `username` as subject, a decision recorded with its cost in
  ADR 0083 (a deleted pod's token is accepted up to 660 s, because TokenReview needs auth-delegator). `username` is the
  right subject under GitOps, since a grant to it can be declared before the account exists. Lakekeeper's guard that a
  Kubernetes authenticator without an audience admits any token an OIDC provider declined (`config.rs:288-309`) is met
  in rask by a fixed audience per door.
- **Assume-role and token roles claim** (`authn.rs:38`, `:964-1118`; `role.fga:12`). A caller narrows a request to one
  role by header, or the IdP's roles claim selects roles. rask's roles are catalog roles granted at its doors (ADR 0192),
  and no client asks to act under fewer roles than it holds; nothing to take until one does.
- **Admission gates** (`crates/lakekeeper/src/service/admission.rs:1-25`). A pluggable post-authentication deny layer for
  an external permission service. rask has no such service; OpenFGA decides.
- **User auto-provisioning and display-name templates** (`crates/lakekeeper/src/api/management/v1/user.rs`). rask keeps
  no user table; a principal exists as the tuples naming it, and offboarding writes one record
  (`services/catalog/src/catalog/services/principals.py:1-30`).
- **Instance admins as an authorization bypass** (`config.rs:792-811`). Folded into finding 2.2 as the authority for the
  `managed_by` lock only. A general control-plane bypass outside OpenFGA is not proposed: rask's estate admin is a tuple
  written from git by the `bootstrap-admin` hook and re-asserted on every sync.
- **Session policy shape** (`crates/lakekeeper/src/service/storage/s3.rs:1328-1459`, `escape_iam_glob_literal` at
  `:1668-1682`). One prefix, three permission levels, IAM wildcards escaped. rask's policy is layout-aware per tier, refuses
  IAM syntax in a prefix rather than escaping it, and bounds the policy's length (`vending.py:352-378`, `:747-900`,
  `:1129-1143`); rask's is narrower. STS session tags (`s3.rs:124-127`) have no counterpart on
  `AssumeRoleWithWebIdentity`, where tags come from the token.
- **Remote signing** (`crates/lakekeeper/src/server/s3_signer`, `s3.rs:664-692`). Iceberg's `signer.uri` protocol;
  lance-namespace defines no signer (`lance_docs/ns_catalog/spec.yaml` has none), and vended credentials with
  `expires_at_millis` are the spec's path (`spec.yaml:2883-2891`).
- **Warehouse validation probe** (`crates/lakekeeper/src/service/storage/validation.rs:44-140`, `VendedCredentialsScopeEnforced`).
  rask runs the same probe at warehouse create and on its validate door, with a conditional-put check Lakekeeper lacks
  (`services/catalog/src/catalog/services/vend_probe.py:1-60`, `warehouses.py:277`, `:1392-1555`). Lakekeeper's CORS and
  STACKIT bucket-policy checks (`service/storage/cors.rs`, `bucket_policy.rs`) are store-specific.
- **Location exclusivity across warehouses** (`validation.rs:62-65`). rask binds a whole bucket to one project
  (`warehouses.py:317-370`) and claims each table location (ADR 0172).
- **Secrets** (`crates/lakekeeper-secrets-kv2/src/config.rs:17-24`, `lib.rs:151-222`; `service/secrets.rs:42-112`).
  Vault KV2 with userpass credentials from environment variables, behind a single-flight cache. rask stores no storage
  credential at all (STS from the catalog's own token, ADR 0170, ADR 0197), reads secrets per app from OpenBao (ADR 0169)
  and re-reads them on a TTL (ADR 0166). Nothing to take; ADR 0070 recorded the same reading.
- **Warehouse status, protection, soft-delete, undrop, expiration, deletion profiles** (`api/management/v1/warehouse/mod.rs:173-223`,
  `warehouse.fga:53`, `:61-65`). rask has activate/deactivate, protection with `force`, a per-warehouse trash grace and
  undrop (`warehouses.py:59-86`, `:454-496`).
- **Read-only maintenance mode** (`crates/lakekeeper/src/service/maintenance_mode.rs:1-8`). Lakekeeper adds a runtime flag
  an instance admin sets, held in memory per process. rask's mode is a setting rendered from git
  (`services/catalog/src/catalog/api/maintenance_mode.py:1-12`), which is the GitOps form; a per-process runtime flag is
  live-only state.
- **Cache TTL jitter** (`crates/lakekeeper/src/service/cache_ttl.rs:1-25`). rask jitters its secret, FGA-model and decider
  caches (`secret_cache.py`, `fga.py`, `decider.py`).
- **Cross-replica cache invalidation by change log, evicting everything on a gap**
  (`crates/lakekeeper/src/service/cache_invalidation/mod.rs:1-4`, `receiver.rs`). rask evicts on the control broadcast
  through a durable per replica (ADR 0182); the binding it caches is immutable and the warehouse status is read live
  (`warehouses.py:413-462`), so a missed eviction is bounded by the binding cache's TTL.
- **Idempotency keys** (`crates/lakekeeper/src/service/idempotency.rs`; the `replay` audit shape). rask's keys are a
  per-door seam scoped by a digest of the subject (`services/catalog/src/catalog/api/idempotency.py:1-21`), and a door's
  authorization runs before its replay, so no unauthorized replay needs its own audit shape.
- **Pagination** (`server/mod.rs:251-333`). Covered by finding 2.5; rask's keyset cursor over a sorted name list is sound
  (`services/catalog/src/catalog/api/pagination.py`).
- **Data contract hook** (`crates/lakekeeper/src/service/contract_verification.rs`). rask judges commits at
  `version/create` and gates publication (ADR 0160, ADR 0161).
- **Endpoint statistics** (`service/endpoint_statistics.rs`). Per-endpoint counts stored in the catalog database; rask's
  metrics go to the OTel Collector.
- **Iceberg-only parts**: views and their security mode (`lakekeeper_view.fga`), generic tables, format-version policy,
  `x-project-id` resolution for header-less Iceberg routes (`service/request_project.rs`), the Iceberg `/config`
  endpoint, and the Postgres task queue.

## 4. Could not confirm

- **That finding 1 reproduces live.** Read from `tables.py:1540-1561` and `fga_deps.py:2120-2135`, `:2478-2493`; I did not
  run a rename against an OpenFGA store. A `model.fga.yaml` check of `manage_grants` before and after a parent-edge move
  would settle it offline.
- **Whether a 4xx from rask's grant or create doors ever names a tuple.** Lakekeeper moved conflicting tuples from the
  response to the log because ids are caller-chosen (`authorizer.rs:1638-1647`). I did not audit rask's problem bodies
  for the same leak.
- **Whether the planned rask operator, or a reconciler Job, is the owner's intended writer of git-declared projects and
  warehouses** (finding 2.2). Not in the repository; the memory note says an operator is planned.
- **Whether any client needs a narrower-than-held role per request** (assume-role). No caller asks for it in this
  repository; looked in `packages/lakehouse/src/lakehouse/governed` and the catalog's doors.
- **Platform STS quotas** (finding 2.6). Depend on the production store, which does not exist yet.
