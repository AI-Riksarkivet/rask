# 0239. One chart value chooses which lineage runs, and the Rust one is built as an image of its own (2026-11-17)

**Status:** accepted; a feature of the port (ADR 0217 point 7: "an implementation is chosen by a chart value in git"),
the slice ADR 0238's "Not done" named. The image is defined, the value renders, the dev estate and the kind lane can
select it, and the read-back is written down. Nothing was built, deployed or read back: this session had no Dagger engine
and no cluster. Source: the owner, 2026-11-17 ("Ship lineage first").

## Context

`lineage serve` starts from the environment `chart/templates/services.yaml` renders for the lineage container and the
flags it passes uvicorn (ADR 0238). Shipping it needs three things git did not hold: an image, a value that runs it in
place of `lineage.main:app`, and a way to read it back on the estate with the suites that judge the Python one.

## Decision

1. **One value, `services.lineage.runtime`: `python` (default) or `rust`.** It selects the container the one Deployment
   `rask-lineage` runs; it adds no Deployment, Service or ServiceAccount, so the Service, HPA, PodDisruptionBudget,
   NetworkPolicy, HTTPRoute, the catalog's grants and the bus user all name the same workload whichever runs. One value
   rather than a second Deployment beside the first, because two lineages on one durable consumer, one relay timer and
   one outbox would be two writers the model allows but nobody has measured, and because switching back is the same
   change in the other direction. The default is `python` because it is the runtime proven on the estate. The schema
   admits only the two words.

2. **What is identical.** Rendered with `rust`, every manifest of the release is byte-identical to the `python` render
   except the lineage container (`tests/unit/test_the_lineage_runtime_changes_only_what_its_process_needs.py`, on the
   production overlay with telemetry on): pod labels, the ServiceAccount, `terminationGracePeriodSeconds`, the spread
   constraints, the pod and container securityContext (read-only root, non-root, no capabilities), ports, the three
   probes, every volume and mount (the `/tmp` emptyDir, the catalog, object-store, secret-store, bus, OTLP and
   ServiceAccount-verifier tokens, the CAs), and every environment variable but five. The flags after the command are
   the same: `--host`, `--port`, `--timeout-graceful-shutdown`, `--proxy-headers`, `--forwarded-allow-ips`, which the
   binary takes (the last two change nothing, ADR 0238).

3. **What differs, and why.**

   | | `python` | `rust` | Why |
   | --- | --- | --- | --- |
   | image | `lance-rest-catalog` (`image.catalog`) | `lance-lineage-rust` (`image.lineage`) | the binary's own image |
   | args | `[opentelemetry-instrument] uvicorn lineage.main:app …` | `serve …` | the binary installs its own telemetry |
   | preStop | `exec: sh -c "sleep 5"` | `sleep: {seconds: 5}` | the image has no shell; the kubelet's sleep action (on by default since Kubernetes 1.30; the dev estate's k3s is 1.36) holds the same 5 s |
   | resources | `resources.lineage` (250m/128Mi, 1/512Mi) | `resources.lineageRust` (250m/64Mi, 1/256Mi) | ADR 0238 measured 14.4 MiB idle and 32.3 MiB (at most 34.5) after its load; request twice that, limit eight times, since no production-sized feed has been measured; CPU kept so the HPA target means the same |
   | env | the chart's | the chart's less `ARROW_DEFAULT_MEMORY_POOL`, `OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER`, `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED`, `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`, `OTEL_SEMCONV_STABILITY_OPT_IN` | read only by pyarrow and the Python OTel distro and instrumentations; the binary exports with the token at `RASK_OTLP_TOKEN_FILE` itself and leaves the probe paths out of its spans in code (`telemetry::http::EXCLUDED`) |

   `MALLOC_ARENA_MAX` stays: the binary links glibc dynamically, whose allocator reads it.

4. **The environment, checked variable by variable** against what `rust/crates` reads (2026-11-17). Every variable the
   `rust` container is given is one the binary reads, with one that it reads and does nothing with: `LINEAGE_DOCS`, since
   the docs pages are not ported (ADR 0238), so `docs.enabled: true` serves no `/docs` from the Rust lineage. The binary
   reads optional variables the chart sets for neither runtime, each defaulting as the Python's setting does:
   `LINEAGE_CATALOG_TIMEOUT_SECONDS`, `LINEAGE_FORWARD_BATCH`, `LINEAGE_FORWARD_TIMEOUT_SECONDS`,
   `LINEAGE_OUTBOX_DRAIN_LIMIT`, `LINEAGE_PUBLISH_TIMEOUT_SECONDS`, `LINEAGE_S3_REGION`, `RASK_FGA_TIMEOUT_SECONDS`,
   `RASK_FGA_ROOT_OBJECT`, `RASK_OIDC_CACHE_TTL`, `RASK_OIDC_LEEWAY`, `RASK_LOG_LEVEL`, `RASK_S3_ENDPOINT_URL`,
   `RASK_S3_CA_BUNDLE`, `OTEL_EXPORTER_OTLP_TIMEOUT` and the per-signal `OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` /
   `_PROTOCOL`. `HOSTNAME` (the relay timer's holder) is the kubelet's. No variable the binary requires is missing from
   the render: ADR 0238's "no new variable" holds.

5. **The image** (`.docker/lineage-rust.dockerfile`, stem `lineage-rust`, image `lance-lineage-rust`, the Python image's
   `rest-catalog` → `lance-rest-catalog` naming). Builder `rust:1.97.0-slim-trixie` by digest (gcc and libc6-dev are all
   the C it compiles: aws-lc-sys builds with the `cc` crate, no cmake or bindgen; `ring` is locked and not built), running
   `cargo build --release --locked -p lineage` over `rust/Cargo.toml`, `Cargo.lock` and `crates/` only, with the cargo
   registry and target directory as cache mounts and the symbols stripped through `CARGO_PROFILE_RELEASE_STRIP`. Runtime
   `gcr.io/distroless/cc-debian13` by digest: what `ldd` lists for the binary built here (libc, libm, libgcc_s, glibc
   2.38 or newer) and the CA bundle rustls reads through rustls-native-certs (`/etc/ssl/certs/ca-certificates.crt`), with
   no shell, package manager or interpreter. The binary is root-owned, run as `USER 10001`, and is PID 1 with no tini: it
   handles SIGTERM itself, exits 0 after its drain and starts no child. A `RUN ["/usr/local/bin/lineage", "operations"]`
   in the shipping stage fails the build if the base lacks a library. `rust/target` is out of the build context
   (`.dockerignore`, `.dagger/images.go`'s ignore list). Dagger's generic `image` function builds it as every stem; the
   Makefile's `k3s-build`/`k3s-import`, `scripts/side_loaded_images.py`, `scripts/new-machine.sh` and the nightly scan
   matrix name it.

6. **A build must be named before `rust` renders.** No release publishes `lance-lineage-rust` (`.dagger/release.go` is
   unchanged), so the global release tag names no image of it; the render refuses `rust` unless `image.tags` or
   `image.digests` names the build, or the images are side-loaded (`image.localImages`). The Makefile's pins check, which
   runs before every `make k3s-up`, keeps to the two images every render runs, and the chart's own refusal covers the
   third where it is run.

7. **The live suites need nothing new.** They reach lineage by its Service and speak HTTP and the bus, so they judge
   either runtime unchanged; `RASK_LINEAGE_RUNTIME=rust make e2e-ci` runs the kind lane on the binary
   (`scripts/stack_values.sh`). Read for what is Python-specific: none of the lineage legs reads a log line, a Python-only
   metric or `/docs`; the one process-level drill, criterion 5's SIGKILL, kills the catalog's uvicorn, not lineage's.
   `test_observability_e2e.py` reads `lineage_events_processed_total` and the span `process lineage.events.v1`, both of
   which the binary exports under the same names (ADR 0238 check (f)). The runbook is
   `docs/architecture/deployment.md` § The Rust lineage.

## What is and is not verified here

- **Rendered**, with the helm `.dagger/charts.go` pins (3.16.4, fetched from get.helm.sh and held to its digest as
  `scripts/k3s-install.sh` fetches it): the local estate's composition (`make k3s-values`' values with a fixed node
  address) and the production overlay are byte-identical to the base commit's under the default, and differ under `rust`
  only as point 3 says.
- **Compiled**: `cargo build --release --locked -p lineage` on this machine (rustc 1.97.0), 23.7 MB, 17.4 MB stripped;
  `lineage operations` runs.
- **Not built**: the image. Its base digests were read from the registries on 2026-11-17; the dockerfile has not run, so
  the `operations` gate, the cache mounts and the image's size are unconfirmed. The Dagger code could not be run.
- **Not deployed and not read back**: nothing on any estate runs the binary. The `sleep` preStop, the memory request and
  every runbook command are unexercised there.
- **Not scanned**: the nightly matrix gains the stem; no scan has run.

## The four facts

- **GitOps:** the runtime is a value in git (`deploy/values-local.yaml` on the dev estate) and the build a pin in git
  (`deploy/values-live-pins.yaml`, written by `make k3s-pins`); `make k3s-up` applies both, and switching back is the same
  path. The render refuses a runtime whose image git does not name.
- **Cloud native:** one declarative Deployment whose container is chosen by a value, an image built by Dagger from a
  dockerfile, platform services named by address as before, and the kubelet's own preStop sleep in place of a shell.
- **Lance-namespace/table-idiomatic:** the Rust lineage reaches every table through the catalog's doors; the image links
  no Lance.
- **Zero trust:** the same ServiceAccount, the same projected tokens, no new secret and no new variable; the image holds
  no interpreter, shell or package manager, and runs non-root on a read-only root.

## Consequences

- A door change still lands in both implementations in one commit (ADR 0217) until the Python lineage is deleted, which
  waits for the read-back and for the decision ADR 0217 point 8 takes on its numbers.
- Adding the image to a release (`releaseImages`), the governance-chain lane (which runs the Python lineage as a Dagger
  service) and the doors-corpus check against an estate are each their own step once `rust` has been read back.
- `make k3s-build` builds three images, the Rust one a cold compile of about five minutes on four shared cores.
