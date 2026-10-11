# chart/templates/gpu-coherence.yaml

Rationale for `chart/templates/gpu-coherence.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## The guard

GPU COHERENCE GUARD: renders no object; it makes an incoherent GPU posture a loud render failure.
`ray.gpuCount` is the chart's one GPU signal (rask.gpuEnabled in _helpers.tpl). A GPU estate whose
Serve fraction is 0 reserves no GPU per replica and runs every replica on CPU while the GPUs sit idle,
which is never wanted and is reported by nothing at run time, so it fails here with the fix in the message.

## `{{- if and (include "rask.gpuEnabled" .) (hasKey .Values.config "RASK_SERVE_GPU_FRAC") (eq (float64 .Values...`

hasKey first: under `helm upgrade --reuse-values` a stored release may carry no such key, and
`float64 nil` is 0 — which would fail a legitimate GPU upgrade for the wrong reason.

## `{{- fail (printf "rask chart: ray.gpuCount=%v (a GPU estate) but config.RASK_SERVE_GPU_FRAC=%v — every Serv...`

NO MODALITY IN A PLATFORM ERROR. This message named a workload and a MODEL, so an operator on
an audio estate hitting a GPU misconfiguration was told about components they do not run — worse
than a vague message, because it sends the reader looking for something that is not there.
