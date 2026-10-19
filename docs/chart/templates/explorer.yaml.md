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

Shared annotation surface (app-id = $svc.daprAppId — viewer/search, the exact
ids the sidecar registers them under); the helper owns the full set so it never drifts.

## `{{- if $root.Values.explorer.corpusS3Root }}`

EACH CORPUS READER PRESENTS ITS OWN SCOPED IDENTITY, never the store's root, which on RustFS signs every STS session
token (ADR 0168). The pod carries the access key id as config and names the Dapr secret that holds its other half:
`MEDIA_S3_SECRET_KEY` is the secret's name and `MEDIA_S3_SECRET_FIELD=secret_key` its field, which
`service_kit.media` fetches fail-closed through the sidecar. The viewer reads `viewer-s3` (`objectStore.viewerAccessKey`,
the object browser's list-and-get identity); search reads `media-s3` (`lance.mediaIdentity`, the
`rask-media` policy: GetObject and a prefix-held list on `explorer.corpusS3Root`, nothing else), since it writes
nothing to the corpus. No other app's Configuration allows either secret and no other app's store login reads it
(`lance.storeReads`), so the identity a pod reads is the one its own row names. Both are set on every reader, though `service_kit.media` defaults to `media-s3` and
`secret_key`, so the render names the identity each pod reads. A blank `objectStore.viewerAccessKey` or
`objectStore.mediaAccessKey` fails the render rather than falling back to a wider credential.

## `{{- $enc := $root.Values.encoders | default dict }}`

`default dict`, not a bare field access: `encoders` is a NEW top-level key, so under
`helm upgrade --reuse-values` the stored values carry no `encoders` at all and
`.Values.encoders.embedUrl` aborts the entire render with "nil pointer evaluating
interface {}.embedUrl". Gated by scripts/verify_encoder_wiring.sh step 2b.

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

## `{{- include "rask.proxyHeaderArgs" $root | nindent 12 }}`

The media pair sit behind the Gateway's data plane (the cluster route's `/api/explorer*` rows), so they
trust its `X-Forwarded-For` from `forwardedAllowIps` alone, exactly as catalog, lineage and the fleet do:
without it every anonymous request carries the data plane's address and the rate limiter keys the whole
public estate as one client.

## `args:`

No `command:`, so the image's tini, its ENTRYPOINT, stays the container's PID 1 and runs uvicorn's command line from
`args` ([[LH-313]]; `services.yaml.md` § `args:`).
