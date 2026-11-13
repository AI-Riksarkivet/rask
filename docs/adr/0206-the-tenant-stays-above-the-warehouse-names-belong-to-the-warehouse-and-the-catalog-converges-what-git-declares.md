# 0206. The tenant stays above the warehouse, names belong to the warehouse, and the catalog converges what git declares (2026-11-13)

**Status:** accepted. Source: the owner, 2026-11-13, in conversation and through the multi-question tool ("Catalog
first, operator later"; "Admit, after #827"; and, of the tenant levels, "if all the others have converged on this and it
makes sense, why should we"). Rows: XC-213 (#827), LH-593 (#828). Amends
[ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md) (its open question "whether
projects, warehouses and grants become resources declared in git", and its direction for
[ADR 0192](0192-a-services-roles-are-granted-at-the-catalogs-doors-scoped-to-the-tenants-it-serves.md)) and the
owner's "Go for operator" of 2026-11-10 (XC-210, #817), which stays planned and comes later.

## Context

Two questions were open.

**Is the project level needed?** A project (admins, members, roles, the quality gate) holds warehouses (one bucket each),
and on the development estate every project held exactly one warehouse. Folding the two into one tenant object was
proposed and then withdrawn once the references were read from source:

| | Tenant level | Storage level | Evidence |
| --- | --- | --- | --- |
| Lakekeeper `a8a3ca1` | project | warehouse | roles belong to a project and are refused outside it (`authz/openfga/v4.15/components/role.fga`); every warehouse privilege reads `… from project` (`warehouse.fga:16-40`); a default project is created at bootstrap (`docs/docs/concepts.md`, § Project) |
| Polaris `4ee02d5d0a1d` | realm | catalog | rows are keyed by realm (`persistence/relational-jdbc/src/main/resources/postgres/schema.sql:46`); principal roles are realm-wide, catalog roles per catalog (`spec/polaris-management-service.yml:380`, `:594`) |
| Gravitino `a37757c5ecf6` | metalake | catalog | "Roles are defined and granted within one metalake" (`docs/metalakes.md:17-20`) |
| Unity Catalog OSS `2611966ca10c` | the server (one metastore) | catalog | "currently the service hosts only one metastore" (`api/all.yaml:1196`) |

All four put the tenant one level above the object that holds storage. In all four, names belong to that storage object:
a namespace is unique per warehouse in Lakekeeper and the warehouse id is the URL prefix; Polaris holds
`UNIQUE (realm_id, catalog_id, parent_id, type_code, name)` (`schema.sql:47`); Unity's catalog is the first segment of a
table's `full_name`; Gravitino's Lance REST server makes the catalog the first namespace segment
(`docs/lance-rest-service.md`, § Namespace Hierarchy). rask differs there: a top-level namespace name is global to the
install and bound, write-once, to one warehouse (`services/catalog/src/catalog/services/warehouses.py:404-429`), so a
caller asking for a name another warehouse holds is answered 409 and learns that it exists.

Lance decides nothing above a namespace. A table's files are placed per table, through its manifest's base paths
(`lance_docs/lance/format/table/layout.md`, § Base Path System); a directory catalog has one root and a table's location
is relative to it (`lance_docs/ns_catalog/catalog/dir/index.md`, manifest table schema); a namespace "can represent a
catalog, schema, metastore, database, metalake", and Lance's own docs map a Unity catalog to the first namespace segment
(`lance_docs/ns_catalog/namespace/index.md`). Its REST routes carry the whole identifier in the path
(`lance_docs/ns_catalog/catalog/rest/index.md`, § REST Routes).

**How is the GitOps gap closed?** Projects, warehouses and grants to services exist only because a door was called, so
an install from git recreates none of them. None of the four references declares them from git: their repositories hold
no operator, custom resource or Terraform provider with code for catalog objects (Lakekeeper's operator lists them as
planned, `docs/architecture/operator-findings.md` § 2). The owner had chosen an operator on 2026-11-10.

## Decision

1. **The tenant levels stay: project above warehouse.** The project is the people-and-policy scope (admins, members,
   roles, the quality gate), the warehouse the storage-and-naming scope. No fold.
2. **A warehouse is the first segment of every identifier** (LH-593): `acme$bronze$t`. The catalog routes a request by
   that segment to the warehouse's root, where its own directory catalog already lives. Names are unique per warehouse,
   the binding registry goes away, and no route changes.
3. **A default project is created at install**, declared in the chart's own values, so a one-team install never has to
   create one (with XC-213).
4. **The catalog converges what git declares** (XC-213). Chart values declare projects, warehouses and grants to
   principals that are not people; the chart renders them into a ConfigMap the catalog mounts; the catalog creates and
   corrects them on a tick through the service functions its doors call. A declared project or warehouse carries
   `managed_by: git` and refuses a spec change through its own doors. Removed from git, a project or warehouse stops
   being managed and keeps its data; a declared grant is revoked, and only the tuple the declaration wrote.
5. **The operator comes later and is thinner** (XC-210): it would write the same declaration from custom resources and
   add a status per object. It is not a prerequisite for GitOps.
6. **Which extra data buckets a warehouse's tables may spread into stays an estate-wide list** until tenants need it per
   warehouse; then it becomes a warehouse attribute, as a Polaris catalog lists `allowedLocations`. No row.

## The four facts

- **GitOps:** met by item 4. The catalog is the reconciler, and a hand change to a declared record is refused at the
  door and corrected on the next tick.
- **Cloud native:** the declaration is a ConfigMap rendered by the chart; no new workload. Per-object status waits for
  the operator.
- **Lance-namespace-idiomatic:** item 2 is the mapping Lance's own docs give for a catalog, and it uses only the spec's
  routes. Project and warehouse stay rask's levels above the spec.
- **Zero trust:** item 4 adds no identity: the catalog is already the authorization store's only client, where an
  operator would hold the estate's operator rung and become admin of every project it creates. Item 2 removes a refusal
  that tells one tenant another's names.

## Consequences

- Every table and namespace identifier gains a segment: tests, seeds, the console and the live suites move with LH-593,
  and existing tables are re-registered under their new identifiers in the same change. Grants are held by uuid, so none
  moves.
- `docs/OPERATORS.md` § 2 and CLAUDE.md's "(Argo CD, the planned rask operator)" are rewritten by XC-213 to say the
  catalog converges and the operator adds status.
- LH-569 (#789, a lock on git-declared records) is folded into XC-213.
- Until the operator exists, whether a declared object has converged is read from the catalog's log and doors, not from
  `kubectl` or Argo CD.

## Not confirmed

- Whether an operator or Terraform provider for any reference exists outside its own repository.
- Whether Polaris resolves the realm anywhere beyond its header resolver and the realm-keyed tables.
