# 0210. The console shows what is there, by its path (2026-11-15)

**Status:** accepted. Source: FE-022 (#830), counted in map #478. Amends
[ADR 0202](0202-the-console-has-one-navigation-and-filters-by-project-in-the-url.md): the sidebar's Catalog group loses
Storage, Operations loses Streams, and Settings loses Audit.

## Context

The console was read page by page on the dev estate on helm rev 17 (2026-11-15), signed in as alice. Several things were
wrong:

- Home was three cards marked "Not wired".
- Four places in the menu could not work, and said so:
  - Streams needs the NATS monitor, which no route serves to a browser.
  - Storage always rendered an HTTP 501.
  - Settings → Audit needs the platform's telemetry store.
  - Settings ended in two "Not wired yet" cards.
- Every catalog object was named by its flat `$` identifier (`acme$bronze$orders`), in headings, titles, lists and
  breadcrumbs. A table's breadcrumb led back to the flat list, not through its namespace and warehouse.
- Nothing told a pipeline how to reach a table, although bring-your-own compute reaching tables through the catalog is
  what the lakehouse is for.
- The table's History tab did not say which version is published.

The reference answers:

- Lakekeeper's console shows counts on its home page and a tree beside object pages. It ends warehouse creation with
  "Connect Compute" snippets, and draws a table's versions with rollback.
- lance-namespace's REST catalog takes the namespace path as a list and the bearer as the `headers.Authorization`
  property (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-rest.md`). `$` is its string-identifier
  delimiter, kept by rask (`lakehouse.naming.CATALOG_DELIMITER`, LH-526).

## Decision

1. **A page the console offers works.** A view that needs what no route serves a browser has no page and no menu row.
   This removes Streams, Storage, Settings → Audit, and the Settings cards for project defaults and credentials. A page
   that can only say it cannot work costs a click and teaches the reader to distrust the menu.
2. **Home is the caller's own summary, read from doors the caller already reads:**
   - their tables per project and tier;
   - lineage's latest commits;
   - the promotions held for review.

   An estate admin also sees the DLQ's depth and the latest governance changes, which the doors refuse everyone else, and
   which are read only for an admin.
3. **A catalog object is named by its path** (`acme › bronze › orders`):
   - in its heading;
   - in its title;
   - in the estate-wide lists;
   - in its breadcrumb, where each ancestor (warehouse, then each namespace) links to its own page.

   A child listed on its parent's page shows its own name. The `$` identifier appears only where it is something to
   copy: beneath the heading with a copy control, in a connect snippet, and in a door's URL.
4. **A table and a namespace have a Connect tab.** It holds a pylance and a LanceDB snippet that reach the object
   through the catalog's Lance REST namespace:
   - the bearer is read from `RASK_TOKEN`;
   - the storage credential is the one the catalog vends on each open;
   - the snippet names the issuer and the audience scope from `/config.json`;
   - it holds no secret.
5. **The History tab is the Versions tab.** Above the commit log a summary names the published version (the one the
   `published` tag names, which the publish door moves) and the latest, and the `published` tag is marked on its
   version.
6. **A tier the console shows is the tier a namespace declares** (owner, 2026-11-15: "Show declared tiers only"):
   `rask.tier`, read with DescribeNamespace from the namespace's path below its warehouse, shallowest first, as the
   catalog's `tier_policy.tier_of` reads it ([ADR 0160](0160-the-tier-judge-owns-the-write-rung-and-a-tier-is-declared-once.md)).
   Nothing reads a tier from a name: a namespace called `bronze` that declares nothing shows no tier, and Home
   draws a tier column only for a tier some table's namespace declares. Tiers are opt-in, and LH-596 (#833)
   replaces them with labels a project defines.
7. **The signed-in person is named** on Home and in Settings with the name `/management/v1/me` gives, and the account
   menu keeps the token's name. Every other principal keeps its full id with a copy control, since no door serves
   another person's name, and the console keeps no directory of its own.

## Consequences

- The menu shrinks by four entries. A view that returns, once a door serves it, comes back as a working page.
- A reader moves from a table to its namespace and its warehouse in one click each.
- No new state, value or backend: the console stays a static app that calls the public doors with the person's bearer.
- Four facts:
  - GitOps: nothing new to declare.
  - Cloud native: no server of the console's own.
  - Lance-idiomatic: paths are lance-namespace's lists, the snippets call its REST client and Lance's own table API,
    and versions and tags are Lance's.
  - Zero trust: tokens stay in memory; every figure is what the caller's own grants allow; the admin reads go only to
    an admin, and every door still decides.
