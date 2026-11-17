# Apache AGE, packaged as a CloudNativePG ImageVolume EXTENSION image (not a full Postgres image).
#
# CNPG ≥ 1.27 mounts this via `spec.postgresql.extensions[].image.reference` as an image volume and appends
# `/extensions/age/share` to extension_control_path + `/extensions/age/lib` to dynamic_library_path — so the
# STOCK CNPG Postgres image stays stock (CVE patches, backups, HA) and AGE rides in as an immutable,
# separately-versioned volume. Requires PostgreSQL 18+ (the extension_control_path feature), K8s 1.33+ with
# the ImageVolume feature gate (1.35 default-on), containerd ≥ 2.1 / CRI-O ≥ 1.31. See docs/CNPG-AGE.md.
#
# AGE is NOT a PGDG Debian package, so we compile it — but the final image is the same `FROM scratch`,
# artifacts-only layout the official cloudnative-pg/postgres-extensions-containers images use. Build FROM the
# CNPG base so the .so is ABI-identical to the Cluster's runtime image (same PGDG PG18 build + distro + arch).
# By digest (read off ghcr.io 2026-11-17, the tag's multi-arch index): a tag alone is mutable, and the extension must be
# built against the same image the Cluster runs (chart/templates/age-cluster.yaml names the same digest).
ARG CNPG_BASE=ghcr.io/cloudnative-pg/postgresql:18-standard-trixie@sha256:37fdeea29a4d3ed14570f719488ba55bd21e8975eaea864be5ce74d25a9a13de
ARG AGE_REF=release/PG18/1.7.0
# The commit that ref names (read with `git ls-remote` 2026-11-17). The ref is a branch, which can move, so the build
# stops unless the clone is this commit; a deliberate bump changes both arguments.
ARG AGE_COMMIT=806fa2ebdb300b3e76ef30cdba61803babbf2683

FROM ${CNPG_BASE} AS build
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git flex bison ca-certificates postgresql-server-dev-18 \
 && rm -rf /var/lib/apt/lists/*
ARG AGE_REF
ARG AGE_COMMIT
RUN git clone --depth 1 --branch "${AGE_REF}" https://github.com/apache/age /age \
 && cd /age \
 && test "$(git rev-parse HEAD)" = "${AGE_COMMIT}" \
 && make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config \
 && make install PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config

# Artifacts-only image: /share/extension/<ext>.control + SQL, /lib/<ext>.so, /licenses/<pkg>/ (CNPG layout).
FROM scratch
COPY --from=build /usr/share/postgresql/18/extension/age*  /share/extension/
COPY --from=build /usr/lib/postgresql/18/lib/age.so        /lib/age.so
COPY --from=build /age/LICENSE                             /licenses/age/LICENSE
# The image is mounted as a volume and never run, so its user is metadata; it names a non-root one so that nothing runs as
# root should anyone start it, and the file's last USER is not the build stage's root (trivy DS-0002).
USER 65532:65532
LABEL io.cloudnativepg.image.base.pgmajor="18" \
      org.opencontainers.image.title="apache-age-cnpg-extension" \
      org.opencontainers.image.version="1.7.0-18"
