# chart/templates/bootstrap-admin.yaml

Rationale for `chart/templates/bootstrap-admin.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

FGA grant bootstrap (goal cond 4): the chart itself seeds the estate's standing tuples instead of an
out-of-band `fga tuple write`. A post-install/post-upgrade hook grants the Dex subject in
auth.bootstrapAdmin two things, on TWO objects, because they are two different authorities:
`owner` on the default warehouse (warehouse:lance_catalog — RASK_FGA_DEFAULT_WAREHOUSE_OBJECT's
default), which cascades down into every namespace and table that warehouse parents; and `admin` on
the estate root (estate:rask — RASK_FGA_ROOT_OBJECT's default), which is what can_observe_events,
can_browse_storage and the admin console gate on. Plus one rung per SERVICE principal that needs a
standing capability, each on whichever of the two its capability belongs to.

THE ESTATE GRANTS ARE SEEDED A RELEASE AHEAD OF THE SETTING THAT READS THEM. This hook runs
post-install/post-upgrade, so the fleet's pods are already rolling when it fires: a release that both
repoints RASK_FGA_ROOT_OBJECT and first writes these tuples hands every estate door a 403 for as long
as the hook takes. While the setting still names a warehouse the estate grants are inert-but-present,
which is exactly what is wanted. The release sequence is what holds the two in that order, and
`make fga-estate-migrate` carries the holders this hook does not know about.

THE SERVICE GRANTS DO NOT DEPEND ON A HUMAN ADMIN BEING NAMED, and this whole Job used to. Its gate
was `and .Values.auth.enabled .Values.auth.bootstrapAdmin`, so an estate that never chose a first
owner rendered no Job at all and every service grant with it. Measured on the live k3s estate
2026-09-10: `auth.bootstrapAdmin: ""`, `kubectl get jobs` listed no bootstrap Job, and the tuples the
notifications reconciler, the sweep and ingest's outbox vend each depend on were unreachable — three
controls that could not fire, behind a values key that has nothing to do with any of them. Who the
first HUMAN owner is, is a deployment's choice; that the sweep may maintain and that ingest may stage
are properties of the software.
There is no fga CLI image, so the Job reuses the catalog image and speaks the OpenFGA HTTP API with
service_kit's own store and model rules, CHECKS each grant first (the idempotence guard), and only
then writes it, treating a duplicate-write 400/409 as success.

## `"helm.sh/hook-weight": "10"`

After every hook this Job waits on: `openfga-model` (0), whose model must hold the relations it writes, and
`minio-scoped-users` (5), whose S3 user the catalog it waits for cannot start without. On a first install the latter is
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
the openfga ingress gets an exclusive client list like openbao/age/minio.

## `- { name: FGA_ROOT_OBJECT, value: "warehouse:lance_catalog" }`

The DEFAULT WAREHOUSE, matching `fga_default_warehouse_object` in
packages/service-kit/src/service_kit/governed/settings.py — the structural root a
top-level namespace hangs off, not the estate root the privileges are checked on.
`tests/unit/test_the_bootstrap_hook_grants_against_the_model_its_image_carries.py`
holds the two apart: it runs this hook's default-render grants against a stub OpenFGA
that refuses a relation its object's own type does not define.

## `- { name: RASK_FGA_STORE_ID, value: {{ . | quote }} }`

The operator-pinned store (env parity with the catalog's RASK_FGA_STORE_ID): the script
prefers this over the name lookup, so a pinned store named anything BUT `lance-catalog`
still gets the bootstrap grant in the right place.

## `{{- $readers = append $readers .Values.medallion.producer.serviceIdentity }}`

THE CASCADE-LAG DETECTOR. It reads two governed surfaces — the source
tables' `published` tag at the catalog and lineage's `/runs` board — to work out how
far a tier has fallen behind. Without the grant both answer 403 and the detector
publishes NO POINT for any edge, which is worse than an absent detector: the empty
series reads as a healthy cascade. Measured live 2026-09-04, where every edge failed
on every tick since the feature shipped.

`reader`, never more: it observes versions and names, and writes nothing.

## `{{- $eventReaders := list }}`

EVENT READERS hold `can_read_event_feed` on the estate ROOT ([[CTL-021]]): lineage's run feed
with no per-dataset filter (`GET /events/projection`), which the notifications reconciler
walks to find whom to tell about runs in EVERY warehouse. The governed `/events` shows a
service only the tables its grants reach, so a tenant warehouse's runs would never arrive.
`event_reader` and nothing wider: no tier, no tenant data, no rung that mints a project or
edits a tuple (`model.fga.yaml` pins both halves). Who is told stays gated per person.
The subject is the value `lance.saSubjects` maps the reconciler's ServiceAccount to at
lineage's door, from the same values key, so the two cannot drift.
