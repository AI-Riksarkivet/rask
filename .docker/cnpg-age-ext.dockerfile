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
ARG CNPG_BASE=ghcr.io/cloudnative-pg/postgresql:18-standard-trixie
ARG AGE_REF=release/PG18/1.7.0

FROM ${CNPG_BASE} AS build
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git flex bison ca-certificates postgresql-server-dev-18 \
 && rm -rf /var/lib/apt/lists/*
ARG AGE_REF
RUN git clone --depth 1 --branch "${AGE_REF}" https://github.com/apache/age /age \
 && cd /age \
 && make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config \
 && make install PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config

# Artifacts-only image: /share/extension/<ext>.control + SQL, /lib/<ext>.so, /licenses/<pkg>/ (CNPG layout).
FROM scratch
COPY --from=build /usr/share/postgresql/18/extension/age*  /share/extension/
COPY --from=build /usr/lib/postgresql/18/lib/age.so        /lib/age.so
COPY --from=build /age/LICENSE                             /licenses/age/LICENSE
LABEL io.cloudnativepg.image.base.pgmajor="18" \
      org.opencontainers.image.title="apache-age-cnpg-extension" \
      org.opencontainers.image.version="1.7.0-18"
