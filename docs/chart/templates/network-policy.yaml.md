# chart/templates/network-policy.yaml

Rationale for `chart/templates/network-policy.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- range (append (splitList "," (include "lance.secretScopes" .)) "openbao" | uniq) }}`

The dev store's readers are the secret-store component's scope itself, `lance.secretScopes` (app-id ==
component label for these pods), so this list and the scope of `lance-secrets` (dapr-component.yaml) cannot
drift, and the store's own pods, because the seed reads the signing pairs and the bus root from the outgoing
pod through the Service (openbao-seed.yaml).

## `{{- if not .Values.openbao.enabled }}`

THE PLATFORM'S STORE, REACHED ONLY BY ITS READERS ([[XC-149]]). It lives outside the namespace, so the lock
is egress: the pods whose sidecar reads it, the same `lance.secretScopes` as the dev store's ingress lock,
reach the addresses the platform states in `openbao.egress`, and no other pod does. Like the API server, the
store is reached at addresses no URL derives, and the render refuses the rule empty under
`networkPolicy.enabled`.

## `{{- $ageClients := list "openfga-migrate" "backup-pg" }}`

stateStore.scopes appended + uniq'd: the state-store-scoped apps' daprd dial this Postgres directly.

## `name: {{ include "lance.fullname" . }}-object-store-ingress`

THE STORE ADMITS ITS NAMED CLIENTS ONLY, by component label: the data-plane services, the scoped-users hook, which creates every platform bucket and every scoped user and so must reach
the store before anything else can, and both backup CronJobs (`backup-pg`, `backup-control-root`), each writing under
`_backups/`. A backup pod absent from the list is dropped in silence under default-deny and its CronJob stays green
while nothing is copied. The rule cannot narrow the admin API, which RustFS serves on the S3 port: that plane is held
by IAM, since no rask identity carries `admin:*` and only the hook holds the root (ADR 0168).

## `{{- $apiClients := list }}`

The pods that call the Kubernetes API, each under the condition its template renders: the token verifiers (catalog
and lineage under `auth.enabled`, reading the service-account issuer's discovery and key set), the issuer mirror,
the dev store's pod (the openbao fixture's; its Kubernetes auth backend reviews ESO's logins), the Collector (`k8sattributes` reads pod metadata), the
volume-snapshot CronJob (`kubectl`) and the Dapr dashboard. Every one holds a token the API server accepts, and no
other first-party pod does; `test_no_egress_rule_admits_every_destination.py` compares the two sets on the
render. The API server is the cluster's: no label selects it and no URL names its endpoints, because the CNI judges
the endpoint after the `kubernetes` Service's translation (host IPs at 6443 on k3s and kubeadm), so the platform
states it as `networkPolicy.apiServerEgress`. An egress rule with no `to` peers matches every destination on its
ports, in the cluster and outside it, so the rule is scoped twice: to these pods, and to those peers.

## `egress: {{- include "lance.egressRules" (list "networkPolicy.apiServerEgress" .Values.networkPolicy.apiServerEgress "the Kubernetes API server's endpoints, at the port a policy sees after the kubernetes Service's translation") | nindent 4 }}`

Every egress rule a deployer states (`networkPolicy.apiServerEgress`, `networkPolicy.extraEgress`, `route.edgeEgress`,
`observability.export.egress`, `observability.query.egress`, `auth.fgaEgress`, `openbao.egress`) passes through this helper, which refuses an empty list
where one is required, a rule with no `to` peers, and a peer `ipBlock` whose CIDR is `/0`: each is a rule that admits
every destination on its ports.

## `name: {{ include "lance.fullname" . }}-allow-openfga-egress`

With `openfga.enabled` off, OpenFGA is the platform's (`auth.fgaApiUrl`, usually HTTPS outside the namespace) and the
estate's authorization fails closed through it. The pods that dial it are the tuple writers (`lance.fgaWriters`), the
model hook and the bootstrap-admin Job, the same list its in-namespace ingress admits, and only they reach the peers the
platform states as `auth.fgaEgress`.

## `name: {{ include "lance.fullname" . }}-allow-dapr-control-plane-egress`

Every sidecar dials the Dapr control plane (Sentry for its certificate, placement, the scheduler, the operator's
API), which is a prerequisite in its own namespace (`dapr.controlPlaneNamespace`, ADR 0153), so the intra-namespace
egress allow no longer covers it. One rule for the whole namespace rather than per component: the plane's ports are
the platform's to choose.

## `name: {{ include "lance.fullname" . }}-allow-telemetry-export-egress`

The Collector exports to `observability.export.endpoint`, the platform's backend, usually outside the namespace;
default-deny egress would drop that silently while the Collector's queue retries into nothing. A URL names a host, and
a NetworkPolicy matches addresses, so the platform states the backend's peers and port as `observability.export.egress`.
`prod-render-check` asserts the stated peer reaches the rule.

## `name: {{ include "lance.fullname" $ }}-allow-telemetry-query-egress`

The zones read the store's SQL and PromQL API at `observability.query.url` (the audit viewer, admin pages and
experiment curves); `observability.query.egress` states its peers and port, for the zone pods only. The zones are the
pods that face the internet, so a port-only rule here would give the most exposed pods a path to any host.

## `name: {{ include "lance.fullname" $ }}-edge-{{ $row.component }}`

One policy per service the cluster route publishes, ranged from the same `rask.apiRoutes` list
`route.yaml` renders its rules from, so a public prefix and an admitted port cannot drift. The Gateway's
data plane lives in the platform's namespace, so the source is unrestricted, as for the zones; the bound is
the PORT: only the app port is admitted, and daprd's ports on the same pod stay intra-namespace. That is
safe because every one of these services authenticates each request itself, and a request through the
Gateway reaches the app port directly, never carrying a sidecar's app token.

## `name: {{ include "lance.fullname" . }}-allow-edge-egress`

The zones' server side sends `/api/*` through the Gateway (`RASK_EDGE_URL` = `route.edgeUrl`), which sits
outside this namespace. A NetworkPolicy sees the data plane's POD port, after the Service's translation
(Traefik on k3s listens on :8000 behind the node's :80), so the rule cannot be derived from the URL: the
platform states it as `route.edgeEgress`, and the render fails without it under `networkPolicy.enabled`
while the zones render.
