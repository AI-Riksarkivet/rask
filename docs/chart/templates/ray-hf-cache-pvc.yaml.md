# chart/templates/ray-hf-cache-pvc.yaml

Rationale for `chart/templates/ray-hf-cache-pvc.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: v1`

The HF model cache, claimed by WHICHEVER Ray object this estate renders.
It lived inside `rayservice.yaml`, so a RayCluster estate mounted `<release>-hf-cache` and nothing
created it — the head would sit ContainerCreating forever on a claim that does not exist. Caught
by the render gate rather than on a cluster, which is the cheap place to find it.

One definition for both consumers, for the same reason `_ray-cluster-config.tpl` exists: the head's
shape is one question, and two copies of it drift where nobody is looking.

## `{{- with .Values.ray.hfCacheStorageClass }}`

NO hardcoded class. `local-path` is the k3s default provisioner's name and does NOT exist on
stock kind (`standard`) or on any managed cluster — hardcoding it wedged this PVC Pending with no
diagnostic, taking the Ray head down with it (live-proof 2026-07-28, defect 2's second site).
Empty => the key is omitted => the CLUSTER's default StorageClass provisions it, which is the only
portable answer. (`storageClassName: ""` would DISABLE dynamic provisioning, hence `with`.)
