# Deploying RustFS for rask

Citations: `rustfs@1.0.3:<path>:<line>` (source at the pinned tag), `docs:<path>` (docs site, English), rask paths bare.

## Three ways to run it on Kubernetes

| Option | What it is | Fit for rask's fixture |
|---|---|---|
| Official Helm chart `helm/rustfs` | published as chart and appVersion 1.0.3 (`https://charts.rustfs.com/index.yaml`, read 2026-10-18; the in-tree `rustfs@1.0.3:helm/rustfs/Chart.yaml:5-6` still reads 1.0.1), classic HTTP repo `https://charts.rustfs.com` (`rustfs@1.0.3:.github/workflows/helm-package.yml:180-210`) | bendable, but most values would be overrides against it (below) |
| RustFS Operator | v0.1.0 pre-release, CRDs `Tenant` (`rustfs.com/v1alpha1`) and `PolicyBinding` (`sts.rustfs.com/v1alpha1`), Kubernetes ≥ 1.30 (`docs:installation/cloud-native/operator/index.md`) | no: pre-release, source not in the clone, and an operator is its own release by rule (ADR 0153) |
| A local fixture chart | about 100 lines rask owns, `deploy/fixtures/rustfs/chart` | **chosen** |

### Why not the official chart

Each point read in source:

- The root credential reaches the pod only as env: `envFrom: secretRef` on `secret.existingSecret` or the rendered
  `<fullname>-secret` (`rustfs@1.0.3:helm/rustfs/templates/deployment.yaml:115-123`;
  `rustfs@1.0.3:helm/rustfs/templates/statefulset.yaml:209-217`). rask keeps secrets out of env; the server itself
  reads files (`rustfs@1.0.3:rustfs/src/config/cli.rs:1551-1564`).
- With `existingSecret`, every key in that Secret becomes an env var named after the key, so rask's ESO keys
  (`object-store-access-key`/`object-store-secret-key`) would not be read as a credential.
- The client Service is always `<fullname>-svc` (`rustfs@1.0.3:helm/rustfs/templates/service.yaml:34`); no value
  yields a Service named `rask-rustfs`. The docs' `kubectl port-forward svc/rustfs` names a Service that does not exist
  (contradiction: `docs:installation/cloud-native/index.md` vs `service.yaml:34`).
- Its Service always publishes the console port 9001, and NodePort mode also opens a console NodePort
  (`rustfs@1.0.3:helm/rustfs/templates/service.yaml:30-85`).
- Unsafe or wrong defaults to override one by one: distributed mode with 4 replicas × 4 drives (16 PVCs)
  (`rustfs@1.0.3:helm/rustfs/values.yaml:8,24,76-88`); `ingress.enabled: true` publishing the console
  (`values.yaml:323-347`); a `busybox:stable` init container, a mutable tag (`values.yaml:57-60`); `serviceAccount.automount: true`
  (`values.yaml:260-264`); `resources: {}`; logs to a PVC at `/logs` instead of stdout (`values.yaml:205-206`).
- Standalone mode needs both `mode.standalone.enabled=true` and `mode.distributed.enabled=false`; both true renders a
  Deployment and a StatefulSet (`rustfs@1.0.3:helm/rustfs/templates/deployment.yaml:6`; `statefulset.yaml:34-38`).
- rask's NetworkPolicy and backup select the store by `app.kubernetes.io/component` on the pod and the PVC
  (`chart/templates/network-policy.yaml:333`; `chart/templates/backup-snapshot.yaml:74`); the chart can stamp that only
  through `commonLabels`, on every object.
- The chart overrides the command with `/usr/bin/rustfs` (`deployment.yaml:102`), skipping the image's
  `entrypoint.sh`, so the entrypoint's empty-value refusal and first-line file read do not apply.
- The chart has no NetworkPolicy template (`rustfs@1.0.3:helm/rustfs/templates/`, directory listing).

What the chart gets right, and the local chart copies: render-time refusal of empty or default credentials
(`rustfs@1.0.3:helm/rustfs/templates/secret.yaml:4-19`), and the pod security context (uid/gid/fsGroup 10001, drop
ALL, read-only root, no privilege escalation, `values.yaml:282-293`). Its README parameter table disagrees with
`values.yaml` on resources, probe delays, secret defaults and ingress hosts (`rustfs@1.0.3:helm/README.md` vs
`values.yaml:128-129,338-347,409,428,440`); trust `values.yaml` and the templates.

## The local fixture chart

`deploy/fixtures/rustfs/fixture.env`: `RELEASE=rustfs`, `NAMESPACE=default`, `SHARED_NAMESPACE=true`, `CHART=chart`,
`WAIT=false` (the root Secret comes from rask's ESO, so the fixture cannot be ready before rask installs). `scripts/fixtures.sh` layers `values.yaml` and `values-live-pins.yaml` and runs
`helm upgrade --install --reset-then-reuse-values`.

| Setting | Value | Why |
|---|---|---|
| Kind | StatefulSet, 1 replica, headless Service `<name>-hl` | stable claim name for the bytes |
| Service | `rask-rustfs`, port `s3` 9000 only, NodePort 30900 | what rask dials; vended endpoint reachable off-cluster (LH-489) |
| Image | `rustfs/rustfs:<GA tag>@sha256:<digest>`, same tag as `.dagger/storage.go:15` | one server version in fixture and CI; no signature to verify, so a digest. The bare tag is the musl build of `Dockerfile`; a `-glibc` suffix is `Dockerfile.glibc`, which also sets the console on (`rustfs@1.0.3:.github/workflows/docker.yml:344-351`; `rustfs@1.0.3:Dockerfile.glibc:119-126`) |
| Volumes | one PVC `data` at `/data`; `RUSTFS_VOLUMES=/data` | one drive (`rustfs@1.0.3:rustfs/src/config/cli.rs:1518-1524`); multiple local paths on one device are refused (`rustfs@1.0.3:crates/ecstore/src/layout/endpoints.rs:1590-1607`) |
| PVC labels | `app.kubernetes.io/instance`, `app.kubernetes.io/component: <store component>` | backup selector |
| Root pair | Secret volume (not optional) with `items` mapping the ESO keys to `access_key`/`secret_key`; env `RUSTFS_ACCESS_KEY_FILE`, `RUSTFS_SECRET_KEY_FILE` | no secret in env (`cli.rs:1551-1564`); set ONLY the `_FILE` pair, each is in a clap group with its inline form |
| Security | pod `runAsUser`/`runAsGroup`/`fsGroup` 10001, `runAsNonRoot`; container read-only root, drop ALL, no privilege escalation | the image's user (`rustfs@1.0.3:Dockerfile:107-125`) |
| SA token | `automountServiceAccountToken: false` | nothing in standalone mode is known to need the API (web identity with a projected token is the exception, `security.md`) |
| `/tmp` | `emptyDir` with a size limit | whether RustFS writes to `/tmp` is not measured |
| Probes | liveness `httpGet /health/live :9000`; readiness `httpGet /health/ready :9000` | `rustfs@1.0.3:rustfs/src/server/prefix.rs:30-49`; the official chart probes liveness at `/health`, an alias of `/health/live`, and readiness at `/health/ready` (`values.yaml:422-444`; `rustfs@1.0.3:rustfs/src/server/layer.rs:1440-1442`) |
| Env | see `config.md` § Fixture env | console off, no update check, stdout logs, minimal health |
| Resources | measure on the kind lane | no source-backed number for RustFS; the fixture's bounds (`deploy/fixtures/rustfs/chart/values.yaml`) are unmeasured |

The product chart selects the store by `app.kubernetes.io/component: rustfs` on the pod and the claim
(`chart/templates/network-policy.yaml`, `chart/templates/backup-snapshot.yaml`), dials it at `lance.objectStoreHost`
(`<fullname>-rustfs`), and provisions its scoped users through the native admin API from rask's own image
(`chart/templates/object-store-scoped-users.yaml`, `scripts/rustfs_admin.py`; `security.md` § Provisioning). No
`MINIO_*` env in the pod: RustFS maps an allowlist of them silently when the canonical name is unset
(`rustfs@1.0.3:crates/utils/src/envs.rs:715-730`).

## CI

- The Dagger lanes run RustFS as a service bound to one exec (`.dagger/storage.go`), with a cache volume owned by
  `rustfs` (`.dagger/storage.go:17-20`). Never docker.
- The kind lane installs the fixture through `scripts/fixtures.sh` (`scripts/e2e_stack.sh`, `fixtures.sh rustfs`).
- Startup: readiness must arrive within `RUSTFS_STARTUP_READINESS_MAX_WAIT_SECS` (default 120 s; `0` means the
  default) or startup returns an error (`rustfs@1.0.3:crates/config/src/constants/health.rs:60-61`;
  `rustfs@1.0.3:rustfs/src/server/readiness.rs:64-74,233-260`; called from `rustfs@1.0.3:rustfs/src/startup_iam.rs:101`).
  That the error ends the process is inferred from the `?` chain, not traced to `main`.
- A data directory on the container's overlay root trips the unsupported-filesystem check, which only warns by default
  (`rustfs@1.0.3:rustfs/src/startup_fs_guard.rs:55-61`; `rustfs@1.0.3:crates/config/src/constants/app.rs:188-197`).
  Give the store a real volume.

## Production topology (the platform's, named not built)

- Modes: SNSD (one drive, EC:0, dev/test), SNMD (one node, several drives), MNMD (multi-node, production)
  (`docs:installation/linux/index.md`). Automatic parity by set width: 1 drive EC:0, 2-3 EC:1, 4-5 EC:2, 6-7 EC:3,
  8-16 EC:4 (`rustfs@1.0.3:crates/ecstore/src/config/storageclass.rs:24-32`); set widths 2..16
  (`rustfs@1.0.3:crates/ecstore/src/layout/disks_layout.rs:24-26`).
- The docs say MNMD needs four servers; the source admits two (`rustfs@1.0.3:helm/rustfs/values.yaml:5-8`): four is
  guidance, not a limit.
- A single-drive deployment can never be expanded; migration off it is through S3
  (`rustfs@1.0.3:crates/ecstore/src/store/init_format.rs:720-740`).
- "RustFS requires XFS" (`docs:troubleshooting/driver.md`) is contradicted: only network, FUSE and overlay filesystems
  are flagged, and only with a warning by default (`rustfs@1.0.3:rustfs/src/startup_fs_guard.rs:55-61`). NFS is
  forbidden by the docs (`docs:installation/linux/prerequisites-and-service.md`).
- Every node needs an identical `RUSTFS_VOLUMES` and the same root keys, and internode RPC rides `:9000`
  (`rustfs@1.0.3:rustfs/src/server/prefix.rs:164`).
