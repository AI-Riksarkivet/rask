# 0033. P4/P7 — backups + structural SPOFs: the prod answer is externalize, not in-chart HA

**Decision.** (Extracted from the retired `GOAL-production-readiness.md`.) The two big structural SPOFs —
RustFS and AGE-Postgres single-replica — are deliberately *not* solved in-chart: that would need an
object-store operator / CloudNativePG, the same class as the parked items. The chart instead wires the
handoff — `rustfs.externalEndpoint` / `age.externalHost` — and `prod-render-check` leg 10 asserts the
RustFS handoff is atomic with the GreptimeDB object-store endpoint (either both set or neither). The
AGE-on-CNPG path is documented and proven (docs/CNPG-AGE.md; CNPG physical PITR supersedes the pg_dump
path). Adopting either = flip the value.

**The open backup gaps that follow** (accepted loss windows until externalized; operational detail in
docs/DURABILITY.md + docs/runbooks/RUNBOOK-restore.md):
- the pg_dump lands on RustFS, so a total RustFS loss loses both the Lance data *and* the DB dumps
  (fate-sharing) — ship the dumps off-cluster, or externalize to CNPG PITR;
- the OpenBao file-backend PVC has no backup path (back up the unseal material out-of-band);
- a documented RPO/RTO and verification that the VolumeSnapshot actually succeeds (the empty
  `snapshotClassName` is a per-cluster value) are still owed;
- lesser SPOFs stay documented, not fixed: the stage runners' single-flight lock is process-local (caps each
  stage at 1 stage runner; a distributed lock is parked until throughput demands it), and Dex is a
  single-replica in-memory IdP (externalize for prod).
