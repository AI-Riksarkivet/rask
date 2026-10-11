# chart/templates/runtimeclass.yaml

Rationale for `chart/templates/runtimeclass.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and (include "rask.gpuEnabled" .) .Values.nvdp.enabled }}`

The nvidia-device-plugin subchart runs its pod under this RuntimeClass (k3s
registers the nvidia containerd runtime but doesn't default it), but the chart
references the class without creating it — so create it here.

Gated on rask.gpuEnabled (ray.gpuCount > 0), the SAME signal that gates every other
GPU-shaped render, not on nvdp.enabled: the Ray head names this class too, so tying it
to the device plugin let a GPU-less estate render `runtimeClassName: nvidia` against a
handler no containerd on the node registers (live-proof 2026-07-28, defect 3/6 — the run
needed a manual --set ray.runtimeClassName=""). nvdp.enabled cannot render without
ray.gpuCount > 0 either — gpu-coherence.yaml fails the render on that pair.
