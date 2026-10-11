# chart/templates/gpu-coherence.yaml

Rationale for `chart/templates/gpu-coherence.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.nvdp.enabled (not (include "rask.gpuEnabled" .)) }}`

GPU COHERENCE GUARD — renders no object; it exists to make an incoherent GPU posture a LOUD
render failure instead of a silent cluster wedge.

`ray.gpuCount` is the chart's ONE GPU signal (see rask.gpuEnabled in _helpers.tpl): the head's
num-gpus + nvidia.com/gpu limit, the nvidia RuntimeClass, each Serve application's GPU fraction
and the Kueue nvidia.com/gpu quota all derive from it. The nvidia-device-plugin (and the GPU
Feature Discovery DaemonSet that ships inside it) is the one consumer that CANNOT: Helm evaluates
a subchart `condition:` against a static values path and has no way to express `gpuCount > 0`.

So the pair is guarded instead. `nvdp.enabled=true` with `ray.gpuCount=0` is the exact
combination that produced live-proof 2026-07-28 defect 6: two DaemonSets stuck
`ContainerCreating` forever on a cluster with no nvidia OCI runtime, contributing nothing and
explaining nothing. It is never a posture anyone wants, so it fails here with the fix in the
message rather than at `kubectl get pods` twenty minutes later.

The reverse pair (`ray.gpuCount>0`, `nvdp.enabled=false`) is LEGITIMATE — that is a cluster whose
GPUs are advertised by an externally-managed device plugin (the NVIDIA GPU Operator, or a cloud
provider's) — so it is a NOTES warning, not a failure.

## `{{- if and (include "rask.gpuEnabled" .) (hasKey .Values.config "RASK_SERVE_GPU_FRAC") (eq (float64 .Values...`

hasKey first: under `helm upgrade --reuse-values` a stored release may carry no such key, and
`float64 nil` is 0 — which would fail a legitimate GPU upgrade for the wrong reason.

## `{{- fail (printf "rask chart: ray.gpuCount=%v (a GPU estate) but config.RASK_SERVE_GPU_FRAC=%v — every Serv...`

NO MODALITY IN A PLATFORM ERROR. This message named a workload and a MODEL, so an operator on
an audio estate hitting a GPU misconfiguration was told about components they do not run — worse
than a vague message, because it sends the reader looking for something that is not there.
