Subject: rask is handing the Kueue CRDs your htr-batch lane uses over to your kueue-system install

Hi — rask is removing its bundled copy of Kueue, and your htr-batch jobs sit on the resources it
owns today, so we want to agree a short window with you first.

WHAT IS TRUE TODAY (read from the cluster, 2026-09-25)
- Two Kueue controllers run on this cluster:
  - yours: kueue-system/kueue-controller-manager, v0.19.0 (applied directly, not a Helm release);
  - ours: default/rask-kueue-controller-manager, v0.18.1 (installed by the `rask` Helm release).
- All 11 kueue.x-k8s.io CRDs are owned by the `rask` Helm release and are at the v0.18.1 schema.
  Five of them (clusterqueues, cohorts, localqueues, multikueueclusters, workloads) send version
  conversion to default/rask-kueue-webhook-service — our controller, not yours.
- Both installs register admission webhooks for Kueue objects (kueue-* and rask-kueue-*).
- Your lane: 8 Workloads in htr-batch (2 admitted on htr-batch-cq, 6 pending), LocalQueue htr-batch,
  ClusterQueue htr-batch-cq.

WHY WE ARE DOING IT
rask does not use Kueue (its own queue has admitted 0 workloads). The bundled CRDs are about a quarter
of rask's Helm release size limit, which has blocked our deploys three times. And if we simply
switched Kueue off in our chart, Helm would delete the 11 CRDs and every Workload with them — yours
included. So it has to be a handover, not a removal.

THE STEPS, IN ORDER (we run them; each is reversible until step 4)
1. Mark all 11 CRDs `helm.sh/resource-policy: keep`, so no rask upgrade can delete them.
2. Point the 5 conversion webhooks at kueue-system/kueue-webhook-service. Your controller's built-in
   certificate management fills in the CA.
3. Check with you that your 8 Workloads, htr-batch-cq and the htr-batch LocalQueue read at both
   v1beta1 and v1beta2, and that your controller still admits.
4. Switch Kueue off in rask's chart. That removes our controller, our two webhook configurations and
   rask's own `rask` ClusterQueue/LocalQueue/ResourceFlavor. The CRDs stay.
Rollback for step 2: point conversion back at default/rask-kueue-webhook-service, which keeps running
until step 4.

WHAT WE NEED FROM YOU
a) A window for steps 2–3, and someone watching the htr-batch Workloads during it.
b) Confirmation that your kueue-system install owns the CRDs from then on. After step 4 they carry no
   Helm owner; you can adopt them (e.g. `helm install ... --take-ownership`) or apply the v0.19.0 CRD
   manifests yourselves — worth doing either way, since they are still at the v0.18.1 schema.
