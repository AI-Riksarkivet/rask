# 0196. An install from git converges under Argo CD, and a changed input converges on the next sync (2026-11-07)

**Status:** accepted. Source: [[XC-164]] (#658), the design audit of 2026-11-01 that widened it, and the owner's rulings
of 2026-11-07: Argo CD is the one GitOps reconciler rask targets ("Fuck flux, only Argo cd if you use any of them"),
and each generated dev credential becomes a Secret of its own, carried over on the dev estate. Refines
[ADR 0182](0182-each-catalog-replica-hears-the-control-broadcast-through-a-durable-of-its-own.md) on the stream Job's
bus permissions.

## Context

Measured at main `5f5810964` on offline renders and in Argo CD v3.5.7's source (gitops-engine, in-tree):

- Argo CD renders the chart with `helm template`: every sync is an install at revision 1, `lookup` answers nothing,
  and Helm's `pre-upgrade` maps to PreSync, which runs before the sync applies anything (`pkg/sync/hook/helm/type.go`).
  `openfga-model` (`post-install,pre-upgrade`) therefore waited in PreSync for a ServiceAccount and an OpenFGA the
  sync had not applied, and the first sync never finished.
- Argo CD leaves hooks out of the state it compares (`pkg/sync/reconcile.go` `splitHooks`), and a sync wave is a
  health gate (`pkg/sync/sync_context.go`: the next wave waits for this one to be Healthy).
- The NATS stream Job created each stream once; the Dapr pod digest covered pub/sub Components alone; the bootstrap
  hook granted and never took a service grant back on the default warehouse; Deployments rendered `replicas` beside an
  HPA; the dev estate's image tags lived in a gitignored file; the generated dev credentials were one ESO Secret whose
  key set could never grow.

## Decision

1. **Every bootstrap Job is an Argo CD hook in the phase it belongs to** (`lance.reconciledJob`): `openfga-migrate`,
   the stream Job, the scoped-users provisioning and `openfga-model` run in `Sync`, the scoped-users prune and
   `bootstrap-admin` in `PostSync`, each with `BeforeHookCreation`, so a sync runs them again and a Job its TTL reaped
   is no drift. Argo CD reads its own hook annotation in place of Helm's on the same object
   (`pkg/sync/hook/hook.go` `Types`), so Helm runs them as before. No sync wave orders them after the ExternalSecrets
   they mount: those secrets come from a store the same release seeds, so a gate before them would hold the first sync;
   each Job's own wait orders it. Names stay revision-named (Lakekeeper's chart does the same, `db-migration.yaml`);
   naming by a hash of the inputs stays [[XC-160]]'s.
2. **A changed hook input is a change Argo CD sees**: `<release>-bootstrap-inputs` is a tracked ConfigMap holding, per
   bootstrap template, a digest of its Jobs' specs.
3. **Every declared stream converges in place**: the stream Job writes each stream's whole config, creates the stream
   from it or edits the stream to it, and fails with the server's reason where JetStream refuses the edit. Its bus user
   may now update each stream it converges (`$JS.API.STREAM.UPDATE.{LINEAGE,DLQ,CATALOG_CONTROL,CATALOG_CONTROL_BROADCAST}`),
   by name, where ADR 0182 named the broadcast mirror alone.
4. **A Dapr pod's digest covers every Component and Resiliency its sidecar loads** (`checksum/dapr`), all rendered by
   `dapr-component.yaml` and `dapr-resiliency.yaml`, so an edit rolls exactly the pods that load it.
5. **The release's own accounts converge to what it declares** on every object a service grant lands on: the hook on
   the estate root and the default warehouse, the catalog's boot backfill on every warehouse it holds, from one
   declaration of the maintainers (`lance.fgaMaintainers`). A person's grant is never taken back.
6. **An autoscaled Deployment renders no `replicas`**, so the HPA owns the count.
7. **Git names every image an estate runs**: the dev estate's pins are committed (`deploy/values-live-pins.yaml`), a
   released chart pins its image by digest, and a real deployment is refused a first-party registry image at the `dev`
   tag. A release ships the catalog image alone, so a production install of one runs without the console until a
   console image is released.
8. **One generated Secret per dev credential** (`lance.devCredentials`), created once and kept, mounted by the dev
   OpenBao as one directory, so a credential added later is generated and the stores' own are never generated again.

## Consequences

- An install from git under Argo CD reaches Ready with no hand step, and a values change converges on the next sync
  (`dagger call` of the GitOps lane installs Argo CD on an empty k3s and checks both).
- Under Argo CD every sync re-runs every bootstrap Job; each is idempotent, so a sync of an unchanged estate changes
  nothing but costs the Jobs' run time.
- Production provisioning (the store's roles and policies, the bus permission table, the signing pairs) is still
  applied by the platform's operator and attested in values; reconciling it continuously is the planned operator's
  ([[XC-162]]).
