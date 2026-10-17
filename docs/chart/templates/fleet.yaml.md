# chart/templates/fleet.yaml

Rationale for `chart/templates/fleet.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $lakehouse := list "catalog" "lineage" }}`

Plane ownership (lance-ns merge P4 — the render-time collision guard, risk 4; docs/adr/0124-chart-unification-one-control-plane-each-one-object-store.md): this template
owns ONLY the rask fleet entries of .Values.services. The lance-ns lakehouse entries (catalog,
lineage) are rendered BY NAME by templates/services.yaml under the same resolved fullname
(`rask-<name>` — rask.fullname and lance.fullname both resolve to "rask" at install), so they
are excluded here UNCONDITIONALLY: no service may render from two templates under any values
combination. A new lakehouse entry in the services map must be added to this list; a new rask
fleet entry needs nothing.

## `{{- if and (not (has $name $lakehouse)) (or $svc.frontDoor $root.Values.singleTenant.enabled) }}`

`frontDoor: true` = part of the DEFAULT install, regardless of singleTenant. A gate that names one
service literally leaves every other front-door service unrendered unless someone sets the LEGACY
single-tenant flag, and the cluster route publishes a prefix for every front-door service
(`rask.apiRoutes` keys on the same `frontDoor` / `singleTenant` condition) — so a default install would
publish a prefix whose backend never rendered (measured 2026-08 on compute's /api/ray, which the
compute fixture chart now serves outside this chart, ADR 0155).

## `terminationGracePeriodSeconds: {{ $root.Values.lifecycle.terminationGracePeriodSeconds }}`

The drain budget, from the same `lifecycle` block as preStop and the sidecar block —
see the arithmetic in values.yaml. These pods declared none, so they silently took the
kubelet's 30s default: a number nobody chose and no comment describes, on pods that all
run a Dapr sidecar whose flush the default does not budget for.

## `{{- include "lance.spreadConstraints" $name | nindent 6 }}`

Every fleet Deployment spreads its replicas across nodes, as the lance-plane ones do. Without
it, a service scaled to two can land both pods on one node, and that node's failure takes the
service down with a PodDisruptionBudget that permitted nothing — the exact failure the helper's
own docstring records. Harmless at one replica.

## `{{- include "lance.securityContext" $root | nindent 10 }}`

THE HARDENING values.yaml ALREADY CLAIMED THESE PODS HAD. Its comment says pod hardening
is "applied to every app container via the lance.securityContext helper" — and this file
did not call it, so the fleet Deployments rendered with no securityContext at all while
every lance-plane and web Deployment had one.

The images already end `USER app`, so the missing `drop: ["ALL"]` was nominal — a
container that never runs as uid 0 holds no effective capabilities either way. What was
genuinely absent is what the CLUSTER enforces rather than what the image promises:
admission-time runAsNonRoot, a RuntimeDefault seccomp profile, and a read-only rootfs.

## `volumeMounts:`

And the scratch pair, so a read-only rootfs is SURVIVABLE: these images write to /tmp for
OTel and pyarrow exactly as the lance ones do. Hardening without this crash-loops on the
first spill instead of failing at deploy time.

## `{{- if $svc.ambientStorage }}`

THE FLEET SECRET GOES ONLY TO SERVICES THAT USE IT, and until 2026-09-08 it went to
every one of them. It carries four keys — AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
HCP_ENDPOINT, HF_TOKEN — and the AWS pair is the store's ROOT credential.

MEASURED INSIDE THE RUNNING PODS: `rask-ingest` listed 106 buckets with it (the whole
estate) while `rask-maintenance`, on its scoped identity, was denied the same call. So
the widest storage credential in the estate was mounted into the
notifications inbox, the compute shell and the flows backend — none of which
constructs an S3 client at all: zero imports of lance, pyarrow, boto3, storage or any
service_kit.lakehouse storage module, no HF code, and no HF/boto dependency. Nothing
in them can make the call the credential authorises.

IT IS ALSO THE FORBIDDEN MECHANISM. Owner ruling 2026-09-08: "Never secret through
envs. Either from ESO, secret store dapr and STS for zero trust" — the rule CLAUDE.md
already states. A service that writes Lance takes a table-scoped vend from the catalog
(`vending.build_session_policy`, by bucket + prefix) and needs no ambient credential.

DEFAULT OFF, and no fleet service declares it: the declaration exists so that a service
which genuinely needs the root pair says so in values, where a review sees it, rather
than receiving it by default.

## `- { name: RASK_AUDIT_ENABLED, value: {{ hasKey $root.Values.auth "audit" | ternary $root.Values.auth.audit ...`

The #41 compliance trail, for the FACTORY-BUILT half of the fleet. `auth.audit`
reaches catalog and lineage as `LANCE_AUDIT_ENABLED` (services.yaml), which arm the
`lance.audit` logger themselves; every service in this loop leaves it at NOTSET and
`make_service_app` applies this flag instead — so without the row the operator's one
lever silences only those two and the rest keep emitting. hasKey+ternary, not `| default`:
Sprig's default() treats boolean false as empty, and a bare reference renders "" on a
`--reuse-values` upgrade from a release predating the key, which is a pydantic
bool_parsing crash at boot. Same convention as LANCE_AUDIT_ENABLED.

## `{{- if and $root.Values.dapr.enabled $svc.daprIngest }}`

APP_API_TOKEN, rendered only for a service with `daprIngest`, independent of whether it writes Lance.
`notifications` is why: it writes no Lance and its bus handler calls `assert_app_token_configured`, so
it rendered with `RASK_DAPR_ENABLED=true` and NO token and raised at import, a chart that renders green
and a pod that never starts.

A `daprIngest` service HOSTS a sidecar-delivered route and FAILS CLOSED without the token
(`service_kit.governed.dapr_auth.assert_app_token_configured` raises whenever RASK_DAPR_ENABLED is true
and APP_API_TOKEN is unset). So it is gated on `dapr.enabled` ALONE: that key sets RASK_DAPR_ENABLED in
the configmap AND renders the Secret this reference reads, so the three move together and the pod
cannot come up armed without a key. Adding `auth.enabled` here would crash-loop the pod on an
ungoverned dev stack, which is the estate's DEFAULT combination.

## `{{- include "lance.appTokenEnv" (list $root $name) | nindent 12 }}`

One secret, so the whole plane presents ONE service identity — the same
`{{ .Release.Name }}-dapr-app-token` the catalog, lineage and the
maintenance cron read. It cannot dangle: `dapr.enabled` is what renders that Secret's
ExternalSecret (external-secrets.yaml) and it also gates this reference.

## `{{- include "lance.governedOidcEnv" $root | nindent 12 }}`

The USER door, for services that declare `governedAuth` — NOT every frontDoor
service. `frontDoor` means "the cluster route publishes it", which is a different
question: a service can be published and still not be governed by the user door, so
keying on it emits variables that service never reads. A governed service's settings carry their own prefix, so
the prefix this env uses is verified per service, not assumed: emitting the wrong prefix produces variables the
service never reads, which looks wired and does nothing.

## `{{- range $k, $v := $svc.env }}`

Per-service env passthrough. Added 2026-08-03 because this block was a FIXED set of
includes: a values key a service needed had nowhere to render, and the first
in-cluster ingest run resolved a RELATIVE warehouse path inside the container as a
result — data would have landed in the pod's ephemeral filesystem and vanished with
it, silently, since a relative Lance path is perfectly valid.
Deliberately opt-in and per-service rather than a global map: config that applies to
everything belongs in the configmap this pod already mounts via envFrom, and a
catch-all here would become the place config goes to avoid a decision.

## `{{- if $svc.channels }}`

WHICH CHANNELS THIS DEPLOYMENT MAY SEND, derived from the SAME `channels.*.enabled`
flags that render the Components in notifications-channels.yaml.

Without this the two halves disagree silently and the feature is inert: enabling
`channels.email` produced a `bindings.smtp` Component, a secret reference and a
scope entry — and a service that never sent anything, because `RASK_NOTIFICATIONS_CHANNELS`
was read by settings.py and written by NOTHING in this chart. `channel_push()`
returns None on an empty list (proxies.py:181-183), so `fan_out` gets no push at
all and every channel send is skipped without a log line. An operator sees a
correctly-provisioned Component and no email, which is the worst shape of failure:
the configuration they touched looks applied.

DERIVED, never a second literal, for the reason the binding name three fields up
already carries: a hand-written `env.RASK_NOTIFICATIONS_CHANNELS` would be a
parallel source of truth that drifts the first time someone flips `enabled`.

## `{{- include "rask.fleetProbes" $svc | nindent 10 }}`

READINESS ASKS /readyz, NOT the badge. Both probes used to point at
`healthPath | default "/api/health"`, and `/api/health` is a STATIC badge — compute's
handler is `return Liveness()`. So the readiness probe answered 200 while the pod was
draining, and the kubelet kept routing to it: the readiness contract inverted.
`service_kit.probes` reports `starting`, `shutting_down` and a service's own hard
dependency, and nothing asked it. `/api/health` is untouched — it is the
frontend-facing badge. The three probes, the boot budget and the measured 5s timeout
now live in ONE helper.

## `{{- toYaml (index $root.Values.resources $name | default $root.Values.resources.fleet) | nindent 12 }}`

A service may name its OWN tier (resources.<name>); otherwise the shared fleet one.

## `args:`

No `command:`, so the image's tini, its ENTRYPOINT, stays the container's PID 1 and runs uvicorn's command line from
`args` ([[LH-313]]; `services.yaml.md` § `args:`).
