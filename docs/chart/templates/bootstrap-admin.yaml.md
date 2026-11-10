# chart/templates/bootstrap-admin.yaml

Rationale for `chart/templates/bootstrap-admin.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

FGA grant bootstrap (goal cond 4): the chart itself seeds the estate's standing tuples instead of an
out-of-band `fga tuple write`. A post-install/post-upgrade hook grants the person's principal in
auth.bootstrapAdmin (`<idp-id>~<user id>`, [[LH-063]]) two things, on TWO objects, because they are two different authorities:
`owner` on the default warehouse (warehouse:lance_catalog — RASK_FGA_DEFAULT_WAREHOUSE_OBJECT's
default), which cascades down into every namespace and table that warehouse parents; and `admin` on
the estate root (estate:rask — RASK_FGA_ROOT_OBJECT's default), which is what can_observe_events
and the admin console gate on. Plus one rung per SERVICE principal that needs a
standing capability, each on whichever of the two its capability belongs to.

THE ESTATE GRANTS ARE SEEDED A RELEASE AHEAD OF THE SETTING THAT READS THEM. This hook runs
post-install/post-upgrade, so the fleet's pods are already rolling when it fires: a release that both
repoints RASK_FGA_ROOT_OBJECT and first writes these tuples hands every estate door a 403 for as long
as the hook takes. While the setting still names a warehouse the estate grants are inert-but-present,
which is exactly what is wanted. The release sequence is what holds the two in that order, and
`make fga-estate-migrate` carries the holders this hook does not know about.

THE SERVICE GRANTS DO NOT DEPEND ON A HUMAN ADMIN BEING NAMED. A Job gated on
`and .Values.auth.enabled .Values.auth.bootstrapAdmin` renders nothing for an estate that never chose a first
owner, and every service grant with it. Measured on the live k3s estate 2026-09-10 under that gate:
`auth.bootstrapAdmin: ""`, `kubectl get jobs` listed no bootstrap Job, and the tuples maintenance depends on
were unreachable, behind a values key that has nothing to do with them. Who the first HUMAN owner is, is a
deployment's choice; that maintenance may maintain is a property of the software.
There is no fga CLI image, so the Job reuses the catalog image and speaks the OpenFGA HTTP API with
lakehouse's own store and model rules, CHECKS each grant first (the idempotence guard), and only
then writes it, treating a duplicate-write 400/409 as success.

## `"helm.sh/hook-weight": "10"`

After every hook this Job waits on: `openfga-model` (0), whose model must hold the relations it writes, and
`object-store-provisioning` (5), whose canned policy for the catalog's ServiceAccount the catalog it waits for cannot
open a store session without. On a first install the latter is
`post-install` only, and at one weight Helm runs the two by name, this Job first, so the install timed out on it
(XC-133, CI run 38217921913). Pinned by `tests/unit/test_the_authorization_model_reaches_the_store.py`.

## `ttlSecondsAfterFinished: 3600`

Reap the finished pod. Each revision's job carries a -r<revision> name, so Helm's
before-hook-creation delete policy never matches the previous one — without a TTL every
`helm upgrade` left another Completed husk behind (14 were sitting in `kubectl get pods`).

## `labels:`

The explicit component label is the P4 landmine (docs/adr/0124-chart-unification-one-control-plane-each-one-object-store.md): hook pods without one are invisible to any
component-scoped NetworkPolicy `from` list. Today the openfga pods sit under the general
intra-namespace ingress allow, but the label keeps this hook admissible (and greppable) the day
the openfga ingress gets an exclusive client list like openbao/age/the object store.

## `subject = {{ include "lance.bootstrapAdmin" . | quote }}`

The principal the render accepted (`lance.bootstrapAdmin`), written as the one OpenFGA user it is through
`fga.user_object`, the seam every other tuple writer uses: its key percent-encoded, so a user id carrying `:` or `#` is
one user and never a userset.

## `- { name: FGA_ROOT_OBJECT, value: "warehouse:lance_catalog" }`

The DEFAULT WAREHOUSE, matching `fga_default_warehouse_object` in
packages/lakehouse/src/lakehouse/governed/settings.py — the structural root a
top-level namespace hangs off, not the estate root the privileges are checked on.
`tests/unit/test_the_bootstrap_hook_grants_against_the_model_its_image_carries.py`
holds the two apart: it runs this hook's default-render grants against a stub OpenFGA
that refuses a relation its object's own type does not define.

## `- { name: RASK_FGA_STORE_ID, value: {{ . | quote }} }`

The operator-pinned store (env parity with the catalog's RASK_FGA_STORE_ID): the script
prefers this over the name lookup, so a pinned store named anything BUT `lance-catalog`
still gets the bootstrap grant in the right place.

## `estate_roles = json.loads(os.environ["FGA_ESTATE_ROLES"])`

THE ESTATE ROLES THE RELEASE DECLARES, one JSON object from each account's subject to the machine roles it holds on the
estate root (`lance.fgaEstateRoles`): lineage's `decider` and `governance_prober`, and, with `maintenance.enabled`,
maintenance's `reclaimer` (`can_reclaim_trash`, the rung the catalog's reclaim door
`POST /management/v1/trash/{kind}/{uuid}/credentials` checks, [[XC-084]]), each on the subject the door verifies its
token as (`lance.saSubject`), so the two cannot drift. Every grant names the user its subject is
(`lakehouse.governed.fga.user_object`, `:` written `%3A`). A client's roles are not here ([[LH-563]]): a client is
granted them at the catalog's access doors as its own identity, `decider` scoped to the projects or warehouses it serves,
or on the estate root at the estate admin's tuple door for one serving every tenant, such as a notifier's
`event_reader` (`can_read_event_feed`, [[CTL-021]]: lineage's run feed with no per-dataset filter,
`GET /events/projection`). The release's own accounts (`lance.ownAccounts`) arrive beside it as `FGA_OWN_ACCOUNTS`.

## `for relation in sorted(machine_relations(target.split(":", 1)[0])):`

THE RELEASE'S OWN ACCOUNTS CONVERGE TO WHAT IT DECLARES, AND NO OTHER PRINCIPAL IS TOUCHED. The grant loop above only
adds, so on its own a role taken from lineage's list, or the maintenance rung once `maintenance.vendWriteCredentials` or
`maintenance.enabled` is off, or maintenance's `reclaimer` once `maintenance.enabled` is off, would keep standing for as long as the store lives: a grant no file in git declares, which
an install from git would not reproduce (GitOps, [[XC-164]]). On each object the hook grants on (the estate root and the
default warehouse), the machine relations are that type's relations only a user holds directly (`[user]`: the estate's
`decider`, `event_reader`, `governance_prober`, `member_expander`, `reclaimer`; the warehouse's `decider` and
`maintenance`), read off
the model this image carries (`lakehouse.governed.fga.machine_relations`), the rule the catalog's offboarding door asks
too. The hook reads every tuple of each on that object, following OpenFGA's `continuation_token` page by page, and
deletes each one naming one of the release's own ServiceAccounts that the release does not declare there. A tuple
naming any other principal was made at a door, the estate admin's tuple door or a project's or warehouse's access door,
and stays: the door and the hook never write the same grant, so an upgrade never undoes a grant a door made (ADR 0192).
The own accounts come from the list of ServiceAccounts the release renders, never from a name prefix, since a client's
ServiceAccount may carry the release's prefix. `admin`, `owner` and every person's rung are never read. A failed read
or delete exits non-zero and fails the hook, so a convergence that did not happen is never reported as done. The
warehouses a tenant mints at runtime carry the same maintenance rung, written by the catalog, which converges it the
same way at boot (`catalog.services.cascade_backfill`).
