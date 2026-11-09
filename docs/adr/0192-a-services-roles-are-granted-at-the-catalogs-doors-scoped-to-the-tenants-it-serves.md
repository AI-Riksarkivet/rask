# 0192. A service's roles are granted at the catalog's doors, scoped to the tenants it serves (2026-11-06)

**Reopened 2026-11-09** by [ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md): grants made only at the doors lose to GitOps; this record stands until the planned operator is designed.

**Status:** accepted. Source: LH-563 (#771). The owner ruled "Grants via doors" on 2026-11-06, after the client-move audit
and the parity audit against Lakekeeper `9124ea8`, and chose the two open points through the multi-question tool the same
day: "Refused, as absent" (how the decision door answers an object outside its caller's scope) and "Revoke, re-grant
scoped" (the client grants the old values had made on the live estate). Amends [ADR 0174](0174-the-provenance-namespace-is-maintained-as-every-table-is.md)
(the compaction job is granted at the namespace's door, and no boot revokes it),
[ADR 0189](0189-the-product-chart-carries-no-engine-seam.md) and
[ADR 0190](0190-the-notifications-inbox-is-a-client-of-the-lakehouses-events.md) (`auth.externalDeciders` is gone).
Folds CTL-019 (#129).

## Context

Read off main `0faa294c3` and rask helm revision 12 on 2026-11-06:

- **A service got its lakehouse permission only from the lakehouse's install values.** `auth.externalDeciders` mapped a
  ServiceAccount to the estate roles `decider`, `member_expander` and `event_reader`, which the bootstrap-admin hook
  wrote on `estate:rask`. `services.lineage.provenanceMaintainers` named the accounts the catalog granted `reader` and
  `maintainer` on the provenance namespace at boot. The live release's values held compute's and the notifications
  inbox's accounts in the first and table-maintenance's in the second, so the lakehouse named its clients (owner,
  2026-11-06: "Lakehouse don't know anything about this services").
- **Two writers with opposite outcomes.** The hook revoked every machine role on the estate root the release did not
  declare (XC-188), and each catalog boot revoked every `writer` or `maintainer` on the provenance namespace its settings
  did not name (ADR 0174). A grant made through the catalog's doors was erased by the next upgrade or boot.
- **The decider could not be scoped.** `decider` existed only on the estate root, so its holder could ask what any
  principal may do on any object in the estate.
- **`data_admin` reached nothing.** CTL-019's three project-admin relations: LH-557 gave `security_admin` and
  `role_creator` the role doors, and `data_admin` stayed a relation no rung or door read.

The reference answers, read from source on 2026-11-06:

- **Lakekeeper** (main `e230d00`) declares no assignment in its chart. Roles go to a principal through its management API
  (`/management/v1/permissions/{server,project,warehouse,…}/assignments`, `crates/authz-openfga/src/api.rs`). A check about
  another principal needs `can_read_assignments` on the object checked (`crates/authz-openfga/src/check.rs:46-61`), which
  each object derives from its grant rungs: a project's `security_admin`, a warehouse's `manage_grants`, inherited down
  (`authz/openfga/v4.15/components/*.fga`). Its single check answers a table the caller cannot see as `TabularNotFound`,
  as a missing one (`crates/lakekeeper/src/service/authz/table.rs:616-692`, `:340`). Its batch check refuses the whole call
  when one object lies outside the caller's reach (`crates/authz-openfga/src/authorizer.rs:1474-1505`,
  `CannotInspectPermissions`). Its reconcile never touches a grant, a role assignment or a bootstrap tuple
  (`crates/authz-openfga/src/reconcile.rs:33-34`).
- **Polaris** (`a10e2df`) grants a service principal principal roles, which hold catalog roles carrying one catalog's
  privileges, all through its management API (`spec/polaris-management-service.yml`). No door answers what another
  principal may do.
- **Gravitino** (`463296c`) grants privileges to roles per metadata object and roles to users through
  `/metalakes/{metalake}/permissions/…` (`docs/open-api/openapi.yaml`). No door answers what another principal may do.
- **`lance_docs`**: the namespace spec defines a request's identity (an API key or a bearer) and `PermissionDenied`
  (code 15), and no grant or decision surface (`ns_catalog/namespace/operations/errors.md`,
  `ns_catalog/namespace/operations/models/Identity.md`, `ns_catalog/catalog/rest/index.md`). Authorization is the
  implementation's.

## Decision

- **A service's roles are grants to its own identity made at the catalog's doors.** No chart value grants one:
  `auth.externalDeciders`, `services.lineage.provenanceMaintainers` and `LANCE_PROVENANCE_MAINTAINERS` are removed. A
  client declares its grants in its own repository and applies them at the doors as a principal allowed to grant (later,
  the rask operator).
- **The decision scope is a relation on project and warehouse.** `project#decider` and `warehouse#decider` are `[user]`
  alone. `can_decide_access` is `decider or security_admin` on a project, `decider or can_decide_access from project` on
  a warehouse, and `can_decide_access from parent` on a namespace, a table and a materialized view. The estate root keeps
  `decider` for a service serving every tenant: lineage's, which the hook writes, or a client's granted at the estate
  admin's tuple door.
- **The decision door's check answers inside its caller's scope.** It asks the estate rung once, then each asked
  object's own `can_decide_access` in one batch, and decides only the objects in scope. An object outside the scope is
  answered refused without being asked, as one that does not exist is. The check route keeps no estate-role gate, and
  every call is audited with what fell outside the scope (`outside_scope`). `list-users`, `governed` and `locate` keep
  their estate rungs.
- **The grant doors.** `POST /management/v1/projects/{id}/access/grant`, `/access/revoke` and `/access/list` serve a
  project as the warehouse's doors serve a warehouse, and `decider` is a grantable rung. `can_grant_decider` is a
  project's `security_admin` and a warehouse's `manage_grants`, and a project's `can_revoke_grant` and
  `can_read_assignments` are its `security_admin`, which `admin` implies. Every grant door asks the model before the
  store: a grantee the rung's type restrictions do not admit is refused 400 code 13 (`fga.admits`), so a `decider`,
  which names one account, is never granted to a role's assignees. Asked, the store refuses such a tuple, and its
  refusal reaches the caller as an outage.
- **A converger touches only what git declares.** A machine role is a relation only a user holds directly
  (`fga.machine_relations`, read off the model each image carries). The hook revokes a machine role on the estate root
  only where it names one of the release's own ServiceAccounts (`lance.ownAccounts`, the accounts `lance.serviceAccounts`
  renders) and the release does not declare it. The provenance boot converges the namespace's `writer` for the same
  accounts, rendered to the catalog as `LANCE_OWN_ACCOUNTS`, and no other relation, so a `writer` granted at the
  namespace's door to an account outside the release stands. The offboarding door no longer reads a machine role on the
  estate root as declared by git.
- **CTL-019 is folded.** `data_admin` is deleted, and `security_admin` carries the decision scope and reads the
  project's grants.

## Considered options

- **Refuse the whole call 403 when one asked object lies outside the scope** (Lakekeeper's batch-check guard): rejected
  by the owner. A client reads a 403 as "this service may not ask" and fails every gate closed
  (`service_kit.governed.decider.CatalogDecider`). Events are estate-wide, so one foreign or dropped table in a batch
  would take a scoped client's gates down, and a 403 beside a refusal would tell a scoped service which names exist in
  another tenant.
- **Keep `auth.externalDeciders` as an empty seam:** rejected by the owner's ruling. The lakehouse names no client, and a
  grant has one writer.
- **Recognise the release's own accounts by their name prefix:** rejected. A client's ServiceAccount can carry the
  release's prefix, as the inbox's `rask-sa-notifications` and table-maintenance's `rask-sa-table-maintenance` do.
- **Scope `member_expander` and `event_reader` too:** not taken. The one service holding them, the notifications inbox,
  leaves with XC-195, and the projection `event_reader` opens is the whole feed by nature. Both stay estate rungs, granted
  at the tuple door.
- **A boot that revokes nothing:** not taken for `writer`. Recording lineage is the lakehouse's own, so one of the
  release's own accounts that git stops declaring a writer loses the rung.

## Consequences

- A client's grant outlives every upgrade and boot. It is removed at the door that made it, or by offboarding its
  principal.
- An account the release stops rendering is no longer one of its own, so no later upgrade or boot revokes what it held.
  A release that drops a component offboards the component's principal at the estate admin's door
  (`DELETE /management/v1/access/principals/{principal}`), as it would any departed principal.
- An install from git grants no client anything. On the development estate the notifications inbox holds no estate role
  after this row's read-back, so it targets nobody until XC-195 deletes it (owner, 2026-11-06).
- No tuple is rewritten: the model adds relations and drops `data_admin`, which no tuple named (measured 2026-11-07 on
  helm revision 12: the store's 83 tuples, read through the estate admin's tuple door).
- Pinned by `tests/integration/test_a_check_only_service_decides_through_the_catalog.py` (the scoped door, on a real
  OpenFGA), `services/catalog/tests/test_the_provenance_namespace_is_kept_as_its_settings_declare.py` (no boot revokes a
  door's grant), `tests/unit/test_the_bootstrap_hook_grants_against_the_model_its_image_carries.py` (no upgrade revokes
  one), `tests/unit/test_the_catalog_is_told_who_maintains.py` (the boot converges the hook's list),
  `services/catalog/tests/test_a_role_grant_stays_inside_its_tenant.py` (a decision rung is refused to a role),
  `services/catalog/tests/test_a_departed_principal_holds_no_grant.py` (offboarding revokes a door-made machine role) and
  `model.fga.yaml` (the scope's reach and its grantors).
