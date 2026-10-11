# chart/templates/explorer.yaml

Rationale for `chart/templates/explorer.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $corpus := .Values.explorer.corpus | default dict }}`

`| default dict` + a `default` per key, NOT a bare `.Values.explorer.corpus.mode` — the same
`--reuse-values` hazard this file already documents for `encoders`, and it is not theoretical:
`explorer.corpus` is a NEW key, and a stored release whose user values carry `media: {enabled: true}`
makes helm 3.20 leave `explorer.corpus` NIL, aborting the ENTIRE render with "nil pointer evaluating
interface {}.mode" (reproduced against the live rask release, 2026-07-28). Every read of the block
goes through these variables so no site can regress independently.

## `{{- if not (has $corpusMode (list "emptyDir" "pvc" "hostPath")) }}`

A typo'd mode would otherwise fall through to the emptyDir branch and quietly serve an EMPTY
corpus — the same class of silent-wrong as the hostPath wedge this replaced, only harder to see.

## `{{- include "rask.daprAnnotations" (list $root $svc.daprAppId $svc.port) | nindent 8 }}`

Shared annotation surface (app-id = $svc.daprAppId — viewer/search/annotator, the exact
ids the gateway's route rows invoke); the helper owns the full set so it never drifts.

## `{{- $enc := $root.Values.encoders | default dict }}`

`default dict`, not a bare field access: `encoders` is a NEW top-level key, so under
`helm upgrade --reuse-values` the stored values carry no `encoders` at all and
`.Values.encoders.embedUrl` aborts the entire render with "nil pointer evaluating
interface {}.embedUrl". Gated by scripts/verify_encoder_wiring.sh step 2b.

## `{{- with include "rask.rayDashboardUrl" $root }}`

Serve-native producer DISCOVERY: the assist registry's primary source is the ray
cluster itself — a Serve app whose deployment carries a `labeling` block in its
user_config IS a registered producer (serve_discovery.py). Discovery reads the
DASHBOARD every Ray consumer names (`rask.rayDashboardUrl`; none ⇒ the env is
absent and discovery is off). Producer CALLS go to Serve's HTTP ingress, the
origin studio takes too (`rask.rayServeOrigin`): the external dashboard host serves
the apps (verified live 2026-08-06), in-cluster it is the load-balanced
`-ray-serve-svc:8000` — never the head's own :8000, which reaches one proxy.
MEDIA_ASSIST_BACKENDS and the runner's MEDIA_ASSIST_URL below stay the operator
override: config is intent, discovery is observation.

## `{{- include "lance.corpusVolume" $root | nindent 8 }}`

The corpus volume, by explorer.corpus.mode. NO hostPath ships by DEFAULT (live-proof
2026-07-28, defect 5): `hostPath` with `type: Directory` requires /var/media-corpus to
already exist on the node, and when it doesn't all three pods wedge ContainerCreating with
a kubelet event as the only clue — on a fresh cluster that is the FIRST thing explorer.enabled
does. `emptyDir` starts every pod cleanly anywhere (the corpus datasets are then loaded into
the pod, or read from the catalog); `pvc` is the prod posture; `hostPath` stays available for
the node-staged corpus but is now opt-in AND DirectoryOrCreate, so it cannot wedge.

The source itself lives in `lance.corpusVolume` (_helpers.tpl) and is INCLUDED, not
transcribed: the readers here and the seed writer must resolve to the same volume, and
when they did not, the seed wrote a corpus to a hostPath while these three read an
emptyDir — green probes on both sides, an empty product, for three days.
