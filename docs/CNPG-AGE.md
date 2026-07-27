# Running the AGE graph on CloudNativePG

The lance-ns state of record is Postgres: the **Apache AGE** lineage graph + the **OpenFGA** datastore, today
a single in-chart StatefulSet (`chart/templates/age-postgres.yaml`). For prod you want managed HA + PITR —
CloudNativePG (CNPG). The one blocker is that AGE is a Postgres **extension** (a compiled `.so` + control/SQL
files per PG major) and **stock CNPG images don't ship it**. Two ways to solve it; the first is preferred.

## The headline win (either path)
CNPG does **physical** backups + PITR (barman-cloud → object store). That is strictly **safer for AGE** than
the in-chart `pg_dump` CronJob: physical replication captures the whole data directory, so the "does a logical
`pg_dump` round-trip the AGE graph labels?" hazard — which `scripts/age_restore_drill.sh` exists to prove —
simply **disappears**. Moving AGE to CNPG isn't only HA; it removes a real DR risk. Set
`backups.pgDump.enabled=false` and let CNPG own backups.

## Preferred: AGE as a CNPG ImageVolume extension (stock Postgres image)
Since **Apache AGE reached PostgreSQL 18** (v1.7.0, 2026-01; also 1.8.0), you can use CNPG's declarative
image-volume extensions and keep the Postgres image **stock** (CVE patches, no fork to maintain). AGE rides in
as an immutable, separately-versioned volume.

### Requirements (verify your prod cluster meets these)
- **PostgreSQL 18+** (the `extension_control_path` feature AGE-as-a-volume relies on)
- **Kubernetes 1.35** (ImageVolume default-on) **or 1.33/1.34 with the `ImageVolume` feature gate enabled**
- **containerd ≥ 2.1 / CRI-O ≥ 1.31**, **CloudNativePG ≥ 1.27**
- Managed control planes (EKS/GKE/AKS) often can't toggle alpha/beta feature gates or aren't on 1.33+ yet —
  if so, use the custom-full-image bridge below.

### 1. Build + push the extension image
`.docker/cnpg-age-ext.dockerfile` compiles AGE against the CNPG PG18 base (so the `.so` is ABI-identical to
the Cluster's runtime image) and emits the `FROM scratch` artifacts-only layout CNPG expects
(`/share/extension/age.control` + SQL, `/lib/age.so`, `/licenses/age/`):
```
docker build -f .docker/cnpg-age-ext.dockerfile -t <registry>/age-cnpg-ext:1.7.0-18 .
docker push <registry>/age-cnpg-ext:1.7.0-18
```
Pin the AGE branch to the PG major (`--build-arg AGE_REF=release/PG18/1.7.0`); a PG-major bump means rebuilding
this small image for the new major (the same lockstep the PG-major runbook in `docs/RUNBOOK-restore.md` covers)
— but it's an isolated extension image, not a whole Postgres fork.

### 2. Apply the Cluster + Database CRs
`deploy/cnpg-age-cluster.yaml` — a stock-image `Cluster` with `spec.postgresql.extensions[].image.reference`
pointing at the extension image, `shared_preload_libraries: [age]`, and `Database` CRs that run
`CREATE EXTENSION age` in the lineage DB (OpenFGA gets its own DB, plain SQL). CNPG auto-appends
`/extensions/age/share` → `extension_control_path` and `/extensions/age/lib` → `dynamic_library_path`.

### Proof status (verified 2026-07-20)
Two of the three layers are proven; the third (the CNPG operator reconciling a Cluster) hit an
environment snag, not an AGE one.

1. **The AGE extension + the `extension_control_path` mechanism — PROVEN.** Built the extension image, mounted
   its artifacts into a **stock `postgres:18`** at a non-standard path, set
   `extension_control_path='$system:/extensions/age/share'` + `dynamic_library_path='$libdir:/extensions/age/lib'`
   (exactly what CNPG configures), and ran: `CREATE EXTENSION age` ✓ · `LOAD 'age'` ✓ · `create_graph('lineage')`
   ✓ · cypher `CREATE (:Dataset)` + `MATCH` → 1 vertex ✓ · `extversion = 1.7.0` ✓.
2. **The ImageVolume infra prerequisites — PROVEN reachable.** Stood up a throwaway kind cluster on **K8s 1.34**
   with **containerd v2.1.3** (≥2.1, the CRI requirement) and the `ImageVolume` feature gate enabled — so a
   real cluster can do the mount.
3. **The CNPG operator managing a real Cluster — NOT completed on this host** (deep-dived; it's a host
   gremlin, not AGE). Findings from the investigation, tried across CNPG **1.30 + 1.28** and kind K8s
   **1.31 / 1.33 / 1.34** (containerd 2.1.x, ImageVolume gate on):
   - The operator **binary is fine on this host** — run standalone (`docker run … controller` with an admin
     kubeconfig, as root, `OPERATOR_NAMESPACE=cnpg-system`, no `--leader-elect`) it starts clean and runs.
   - Three real startup gates were diagnosed from the standalone's full error chain (the in-cluster pod
     logs only 3 lines then exits, with the error **never flushed** to the container log): (a) `ensurePKI`
     needs `OPERATOR_NAMESPACE` (else "empty namespace may not be set"); (b) it writes its webhook cert under
     `/run/secrets`, which the **non-root** operator can't (`mkdir /run/secrets: permission denied`);
     (c) `--leader-elect` needs the in-cluster SA namespace file — and mounting an emptyDir at `/run/secrets`
     to fix (b) **hides the SA token** and breaks (c).
   - Despite fixing all three in-cluster (run as root, don't shadow the SA token, loosen the probe, even
     grant the SA cluster-admin), the pod still restart-loops with the error unflushed — a residual
     CNPG-1.30-deployment-on-this-kind issue that resisted reasonable fixes and is **orthogonal to AGE**.
   CNPG runs normally on standard/managed clusters (it's their own CI substrate). Both extension images are
   built (`age-cnpg-ext:1.7.0-18`, `cnpg-pg16-age:bridge`); with (1)+(2) proven, running
   `deploy/cnpg-age-cluster.yaml` on any conformant cluster closes the last mile.

## Bridge (older clusters without ImageVolume): a custom full image
If your prod K8s can't do ImageVolume yet, build AGE into a **custom CNPG Postgres image** and point the
Cluster's `spec.imageName` at it (works on any K8s + any PG major AGE supports, e.g. PG16/17):
```dockerfile
FROM ghcr.io/cloudnative-pg/postgresql:16 AS build
USER root
RUN apt-get update && apt-get install -y build-essential git flex bison postgresql-server-dev-16 \
 && git clone --branch release/PG16/1.5.0 https://github.com/apache/age /age \
 && cd /age && make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config && make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config
FROM ghcr.io/cloudnative-pg/postgresql:16
COPY --from=build /usr/lib/postgresql/16/lib/age.so /usr/lib/postgresql/16/lib/
COPY --from=build /usr/share/postgresql/16/extension/age* /usr/share/postgresql/16/extension/
```
Same handoff wiring; switch to the ImageVolume image when the cluster qualifies (identical `age.externalHost`).

## Chart handoff (already wired + CI-guarded)
Whichever path, the lance chart just points at the CNPG cluster (`values-prod.yaml` EXTERNALIZE block):
```yaml
age:     { enabled: false, externalHost: lance-pg-rw }             # the CNPG -rw (primary) service
openfga: { datastore: { uri: "postgres://lance:…@lance-pg-rw:5432/openfga?sslmode=require" } }
backups: { pgDump: { enabled: false } }                            # CNPG PITR supersedes the pg_dump CronJob
```

## Recommendation
Target the **ImageVolume** path — it's the operator-native, GitOps-friendly answer that keeps Postgres stock.
Gate on (a) your prod K8s meeting the ImageVolume floor and (b) soak-testing AGE-on-PG18 (v1.7.0 is new; or
start on PG16/17 via the bridge and move up). Either way CNPG's physical PITR is the DR upgrade over pg_dump.
