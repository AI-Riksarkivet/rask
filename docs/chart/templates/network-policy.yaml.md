# chart/templates/network-policy.yaml

Rationale for `chart/templates/network-policy.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- range (append (splitList "," (include "lance.secretReaderComponents" .) | compact) "openbao" | uniq) }}`

The dev store's readers are the pods that log in to it (each service, and its sidecar for its components), named by their component labels:
`lance.secretReaderComponents`, which is the secret-store component's scope (`lance.secretScopes`, where an app's id is
its pods' component label) and maintenance's worker pods, which run under maintenance's app id and carry the label
`maintenance-worker`. Derived from the scope, so this list and the scope of `lance-secrets` (dapr-component.yaml)
cannot drift. The store's own pods are admitted too, because the seed reads the signing pairs and the bus root from the
outgoing pod through the Service (openbao-seed.yaml).

## `{{- $platform := include "lance.platformEgress" . | fromJsonArray }}`

EVERY ENDPOINT THE PLATFORM SERVES OUTSIDE THE NAMESPACE IS ONE ROW ([[XC-143]], [[XC-202]]): its value, what it
reaches, the components whose pods dial it, and the condition under which the release dials it there. The API server
whenever a pod calls it; the platform's OpenFGA with `openfga.enabled` off; its secret store with `openbao.enabled`
off; the telemetry backend with `observability.enabled`; its S3 and STS with `objectStore.enabled` off; its IdP with
`auth.enabled` and `dex.enabled` off; and on a real deployment (`rask.isRealDeployment`), where the bus is always the
platform's, its NATS bus and the graph backend `services.lineage.forward.url` names. A URL names a host and a
NetworkPolicy matches addresses (after a Service's translation, for the API server), so the platform states each
endpoint's peers as rules. Under default-deny egress a row with no rules leaves its pods unable to reach the endpoint
while they keep running, so the render refuses before any policy renders, in ONE message naming every empty value
with what it reaches and who dials it: a deployer fixes them all in one pass instead of one per render. The same rows
render the policies below, so the requirement and the policy cannot drift.

## `{{- $ageClients := list "openfga-migrate" "backup-pg" }}`

The Postgres's component-labelled clients: OpenFGA's migrate Job and the pg backup job (OpenFGA's own pods are
admitted by their subchart's name label below). No daprd dials it, since the release renders no Dapr state store.

## `values: {{- toYaml (concat (include "lance.objectStoreClients" . | fromJsonArray) (list "object-store-provisioning" "bucket-events")) | nindent 18 }}`

THE STORE ADMITS EXACTLY THE PODS THAT DIAL IT, by component label: every pod holding a store session
(`lance.objectStoreClients`: the data-plane services, maintenance's workers and the backup Jobs, each writing under
`_backups/`), the same list the platform's S3 is reached from, so the dev store's ingress and the platform's egress
cannot drift; the provisioning hook (`object-store-provisioning`), which creates every platform bucket and every canned
policy and so must reach the store before anything else can; and the store fixture's own `bucket-events` Job, which
configures each bucket's change events with the root. A pod absent from the list is dropped in silence under
default-deny (a backup CronJob stays green while nothing is copied), and a pod present that never dials the store is a
path for nothing but an intruder: the volume-snapshot CronJob calls only the API server, so it is not here.
`test_a_development_store_admits_exactly_the_pods_that_dial_it.py` compares the two sets on the render. The rule
cannot narrow the admin API, which RustFS serves on the S3 port: that plane is held by IAM, since no rask identity
carries `admin:*` and only the hook and the fixture's Job hold the root (ADR 0168).

## `name: {{ include "lance.fullname" $ }}-allow-{{ .name }}-egress`

One policy per platform row, selecting the row's components and admitting them to the peers its value states. The
rows' callers, each under the condition its template renders: the API server's are the token verifiers (catalog and
lineage under `auth.enabled`, reading the service-account issuer's discovery and key set), the issuer mirror, the dev
store's pod (the openbao fixture's; its Kubernetes auth backend reviews ESO's logins), the Collector (`k8sattributes`
reads pod metadata), the volume-snapshot CronJob (`kubectl`) and the Dapr dashboard; OpenFGA's are the tuple writers
(`lance.fgaWriters`) and the model hook, the list its in-namespace ingress admits; the secret
store's are the pods that log in to it (`lance.secretReaderComponents`, as the dev store's ingress lock); the
telemetry backend's is the Collector; the S3's are the pods holding a store session (`lance.objectStoreClients`), whose
STS answers at addresses the same rules name; the IdP's are the doors verifying a person's token (`lance.tokenVerifiers`);
the bus's are every pod whose daprd loads a pub/sub Component (`lance.secretReaderComponents`: each injected pod loads
both components its app is scoped to) and the stream Job (`lance.busClients`); and the graph backend's is lineage, whose
relay forwards to it. `test_no_egress_rule_admits_every_destination.py` compares each row's callers, read off the pods'
own specs, with the pods its policy admits.

## `egress: {{- include "lance.egressRules" (list .value .rules .reaches) | nindent 4 }}`

Every egress rule a deployer states (each platform row's value and `networkPolicy.extraEgress`) passes through this
helper, which refuses a rule with no `to` peers and a peer `ipBlock` whose CIDR is `/0`: each is a rule that admits
every destination on its ports, in the cluster and outside it. An empty row is refused above, where every row is read.

## `- { key: app.kubernetes.io/component, operator: NotIn, values: [console] }`

Every egress allow the namespace shares (DNS, the intra-namespace allow, the Dapr control plane, `networkPolicy.extraEgress`)
selects every pod but the console's. Its static server dials nothing: it reads its config from a mounted file, has no
sidecar and calls no door, the browser making every API call itself. So the console's pod keeps default-deny's empty
egress, and a compromised static server reaches nothing, not even DNS.

## `name: {{ include "lance.fullname" . }}-allow-dapr-control-plane-egress`

Every sidecar dials the Dapr control plane (Sentry for its certificate, placement, the scheduler, the operator's
API), which is a prerequisite in its own namespace (`dapr.controlPlaneNamespace`, ADR 0153), so the intra-namespace
egress allow no longer covers it. One rule for the whole namespace rather than per component: the plane's ports are
the platform's to choose.

## `name: {{ include "lance.fullname" $ }}-edge-{{ $row.component }}`

One policy per service the cluster route publishes, ranged from the same `rask.apiRoutes` list
`route.yaml` renders its rules from, so a public prefix and an admitted port cannot drift. The Gateway's
data plane lives in the platform's namespace, so the source is unrestricted; the bound is
the PORT: only the app port is admitted, and daprd's ports on the same pod stay intra-namespace. That is
safe because every one of these services authenticates each request itself, and a request through the
Gateway reaches the app port directly, never carrying a sidecar's app token.

## `name: {{ include "lance.fullname" . }}-edge-console`

The console's static server is admitted the same way, on its one port (`console.port`), from anywhere: the Gateway's
data plane lives in the platform's namespace, and nothing on the pod authenticates or holds anything a caller could
reach, since it serves the public build and `/config.json`, which holds no secret. Rendered only with
`console.enabled`, like the console itself (`console.yaml`).

## `values: {{- toYaml $busPeers | nindent 18 }}`

THE DEVELOPMENT BUS ADMITS EXACTLY ITS CLIENTS ([[XC-202]]). Its clients on 4222 are the pods the platform's bus is
reached from (`lance.busClients`: every pod whose daprd loads a pub/sub Component, carrying `dapr.io/enabled`, and the
stream Job), the bus fixture's own `nats-box`, and with `objectStore.enabled` the development object store's server
(component `rustfs`), which publishes each change on `store.events` for maintenance's work queue (ADR 0198); a pod that
never dials the bus gets no path to it. The servers admit each other on 6222, whose cluster routes the fixture's
three-node JetStream cluster runs on; without it a cluster under the isolation layer partitions. 7422 (leafnodes) and
the 8222 monitor stay closed: no pod the release renders reads them.
`test_a_development_store_admits_exactly_the_pods_that_dial_it.py` reads every pod of the namespace, the release's and
those of the fixtures installed beside it, and compares both sets on the render.
