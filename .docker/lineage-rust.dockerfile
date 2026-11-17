# syntax=docker/dockerfile:1.11
# The Rust lineage image: the one `lineage` binary of the Rust plane (rust/, ADR 0217, ADR 0238) on a distroless base, run
# by the chart's lineage Deployment under `services.lineage.runtime: rust` (ADR 0239). It holds no Python, no shell and
# no package manager; the Python lineage stays in the lakehouse image (.docker/rest-catalog.dockerfile).
# Build context = repo root:  scripts/dagger-image.sh --name lineage-rust --tag lance-lineage-rust:dev

# ── builder: the workspace's release build of the one binary ─────────────────
# The official Rust image's slim variant carries gcc and libc6-dev, which is all the C the build compiles: aws-lc-sys
# (rustls' crypto provider, under reqwest and async-nats) builds its C with the `cc` crate on linux x86_64 and aarch64
# ("Building with: CC" in its build output, no cmake, no bindgen; measured on aws-lc-sys 0.47.0, 2026-11-17), and no
# other crate in the lineage closure compiles C (`ring` is in Cargo.lock and not built for these targets).
# 1.97.0 is the toolchain the pilot was built and measured with (ADR 0238); the workspace asks for 1.90 or later.
FROM rust:1.97.0-slim-trixie@sha256:686a437ead83701e8f871e66e838c3ec55f46b5fc235b025756396ac823bdc51 AS builder

WORKDIR /src
# Only the Cargo workspace: its manifest, the lockfile the build is held to, and the crates. The vectors, the ledger and
# every other plane stay out of the context the build reads.
COPY rust/Cargo.toml rust/Cargo.lock ./
COPY rust/crates crates

# The registry and the target directory are caches, so a source edit recompiles the workspace's crates and not its
# dependencies; the binary is copied out of the target cache in the same step, since a cache mount ships in no layer.
# Stripped of its symbols as the pilot measured it (17.4 MB), through cargo's profile variable rather than an edit of
# the workspace's release profile.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    CARGO_PROFILE_RELEASE_STRIP=symbols cargo build --release --locked -p lineage \
    && install -D -m 0755 target/release/lineage /out/lineage

# ── final: glibc, libgcc_s and the CA bundle, nothing else ───────────────────
# What the binary needs at run time, read off it (`ldd`, 2026-11-17): libc, libm and libgcc_s (Rust's unwinder), glibc
# 2.38 or newer, and the platform trust store, which rustls reads through rustls-native-certs at
# /etc/ssl/certs/ca-certificates.crt (or SSL_CERT_FILE / SSL_CERT_DIR); a private issuer's or store's CA is a file the
# chart mounts and names. distroless/cc is glibc, libgcc, CA certificates and tzdata with no shell or package manager,
# rebuilt by Google as Debian publishes fixes. The process reads /etc/hostname and the projected token files the
# kubelet mounts, and writes nothing, so a read-only root filesystem holds.
# hadolint ignore=DL3026  # Reason: gcr.io/distroless is Google's official distroless registry; digest-pinned.
FROM gcr.io/distroless/cc-debian13@sha256:809d04afa1be033c441f76bb0051878c342fb76d0ea236bc6dc94b655fa0fbd6 AS final

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION
LABEL org.opencontainers.image.title="lance-lineage-rust" \
      org.opencontainers.image.description="Lance lakehouse lineage service, the Rust binary: lineage serve" \
      org.opencontainers.image.source="https://github.com/AI-Riksarkivet/rask" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${VERSION}"

# Root-owned and not writable by the account that runs it.
COPY --from=builder --chown=0:0 /out/lineage /usr/local/bin/lineage

# ── the start gate ───────────────────────────────────────────────────────────
# The binary is run once in the stage that ships it, so a library the base does not carry fails the build rather than
# the pod. `lineage operations` reads no setting and opens no socket. Exec form: the stage has no shell.
RUN ["/usr/local/bin/lineage", "operations"]

# uid 10001, as the lakehouse's image runs. Numeric, so the kubelet can verify `runAsNonRoot`; the uid needs no passwd entry.
USER 10001
EXPOSE 8000

# NO tini. The binary is PID 1 and needs none: it installs its own SIGTERM handler (a PID 1 ignores only a signal whose
# disposition is the default), drains and exits 0 rather than raising the signal again, and starts no child to reap.
# The chart sets `args` only, so its `serve` and flags follow this entrypoint. No HEALTHCHECK: the image has no client
# to probe with, and the chart's probes ask `/livez` and `/readyz`.
ENTRYPOINT ["/usr/local/bin/lineage"]
CMD ["serve", "--host=0.0.0.0", "--port=8000"]
